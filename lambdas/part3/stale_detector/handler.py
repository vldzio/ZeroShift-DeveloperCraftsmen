"""Lambda entry point for the SCP Stale-Statement Detector.

Iterates every SCP in the org (fixture mode reads the org tree; real mode calls
Organizations), skips Control-Tower–managed policies, and for each Deny
statement queries CloudTrail Lake for matching activity over the lookback
window. Statements with zero activity are flagged as dead-deny findings.

Read-only workflow — never mutates any SCP.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import boto3

from part3.stale_detector.statement_analyzer import (
    StatementFinding,
    analyze_statement,
)
from shared.cloudtrail_lake_client import count_activity
from shared.control_tower_guard import is_ct_managed
from shared.logging_config import get_logger, new_correlation_id
from shared.organizations_client import OrganizationsClient

log = get_logger(__name__)

AUDIT_TABLE_ENV = "ZEROSHIFT_SCP_AUDIT_TABLE"
APPROVAL_TOPIC_ENV = "APPROVAL_REQUESTS_TOPIC_ARN"
DEFAULT_LOOKBACK_DAYS = 180


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    correlation_id = new_correlation_id()
    body = _extract_body(event)
    lookback_days = int(body.get("lookbackDays") or DEFAULT_LOOKBACK_DAYS)

    log.info(
        "stale_scan_start",
        extra={"lookbackDays": lookback_days, "awsRequestId": getattr(context, "aws_request_id", None)},
    )

    org = OrganizationsClient()
    scp_ids = _list_all_scp_ids(org)
    log.info("stale_scan_scps_discovered", extra={"count": len(scp_ids)})

    findings: list[StatementFinding] = []
    scanned = 0
    skipped_ct = 0

    for scp_id in scp_ids:
        try:
            policy = org.get_policy(scp_id)
        except FileNotFoundError:
            continue
        if policy.control_tower_managed or is_ct_managed(policy.name, policy.policy_id):
            skipped_ct += 1
            continue
        scanned += 1
        for statement in policy.document.get("Statement", []) or []:
            if statement.get("Effect") != "Deny":
                continue
            enumerated_actions = _enumerate_actions_for_counts(statement)
            counts = count_activity(enumerated_actions, lookback_days=lookback_days) if enumerated_actions else {}
            finding = analyze_statement(
                scp_id=policy.policy_id,
                scp_name=policy.name,
                statement=statement,
                action_counts=counts,
                lookback_days=lookback_days,
            )
            if finding.is_stale:
                findings.append(finding)

    log.info(
        "stale_scan_done",
        extra={
            "scpsScanned": scanned,
            "scpsSkippedControlTower": skipped_ct,
            "findingCount": len(findings),
        },
    )

    _persist_findings(findings=findings, correlation_id=correlation_id, lookback_days=lookback_days)
    _publish_summary(findings=findings, correlation_id=correlation_id, lookback_days=lookback_days)

    return _api_response(
        {
            "correlationId": correlation_id,
            "lookbackDays": lookback_days,
            "scpsScanned": scanned,
            "scpsSkippedControlTower": skipped_ct,
            "findingCount": len(findings),
            "findings": [asdict(f) for f in findings],
        }
    )


def _list_all_scp_ids(org: OrganizationsClient) -> list[str]:
    """In fixture mode, walk the org tree to find every attached SCP id.

    In real mode, this would call ``organizations:ListPolicies`` filtered by
    SERVICE_CONTROL_POLICY. Real-mode support is deferred; for the demo the
    fixture-mode traversal is used.
    """
    tree = org._load_org_tree()  # noqa: SLF001 — internal but stable for this component

    def walk(node: dict[str, Any], out: set[str]) -> None:
        for scp_id in node.get("scps", []) or []:
            out.add(scp_id)
        for child in node.get("children", []) or []:
            walk(child, out)

    seen: set[str] = set()
    walk(tree, seen)
    # Include the oversized fixture (not attached to the tree, but visible to
    # the refactorer). Add if the fixture file exists.
    seen.add("p-prod-oversized-deny")
    return sorted(seen)


def _enumerate_actions_for_counts(statement: dict[str, Any]) -> list[str]:
    """For staleness we need the concrete list to query CloudTrail. Reuse
    the module-level enumerator from statement_analyzer so the same actions
    end up in both the query and the finding."""
    from part3.stale_detector.statement_analyzer import enumerate_statement_actions

    return enumerate_statement_actions(statement)


def _persist_findings(*, findings: list[StatementFinding], correlation_id: str, lookback_days: int) -> None:
    table_name = os.environ.get(AUDIT_TABLE_ENV)
    if not table_name:
        return
    if not findings:
        return
    table = boto3.resource("dynamodb").Table(table_name)
    now = datetime.now(timezone.utc).isoformat()
    for finding in findings:
        change_id = str(uuid.uuid4())
        item = {
            "scpId": finding.scp_id,
            "changeId#timestamp": f"{change_id}#{now}",
            "changeType": "STALE_DENY_RECOMMENDATION",
            "scpName": finding.scp_name,
            "statementSid": finding.statement_sid,
            "actions": finding.actions,
            "actionCounts": finding.action_counts,
            "lookbackDays": lookback_days,
            "recommendation": finding.recommendation,
            "correlationId": correlation_id,
            "createdAt": now,
        }
        item = {k: v for k, v in item.items() if v is not None}
        table.put_item(Item=item)


def _publish_summary(*, findings: list[StatementFinding], correlation_id: str, lookback_days: int) -> None:
    topic_arn = os.environ.get(APPROVAL_TOPIC_ENV)
    if not topic_arn:
        return
    subject = (
        f"[ZeroShift] Stale-SCP scan: {len(findings)} dead-deny recommendation(s)"
        if findings
        else "[ZeroShift] Stale-SCP scan: no findings"
    )
    body = {
        "correlationId": correlation_id,
        "lookbackDays": lookback_days,
        "findingCount": len(findings),
        "findings": [
            {
                "scpId": f.scp_id,
                "scpName": f.scp_name,
                "statementSid": f.statement_sid,
                "actions": f.actions[:10],
                "totalActivity": f.total_activity,
            }
            for f in findings[:10]
        ],
    }
    boto3.client("sns").publish(
        TopicArn=topic_arn,
        Subject=subject,
        Message=json.dumps(body, indent=2, default=str),
    )


def _extract_body(event: dict[str, Any]) -> dict[str, Any]:
    if "body" in event and isinstance(event["body"], str):
        try:
            return json.loads(event["body"])
        except json.JSONDecodeError:
            return {}
    return event


def _api_response(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }
