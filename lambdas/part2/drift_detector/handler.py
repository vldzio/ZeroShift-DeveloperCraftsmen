"""Lambda entry point for POST /detect-drift.

Scans every managed role, computes drift, generates a proposed replacement
policy per role that has drift, scores the risk tier, writes findings to the
audit table, and returns them inline for the frontend to render.

Does NOT kick off the Step Functions state machine — that happens later via
POST /remediate-role once the user chooses which role to remediate.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import boto3

from part2.drift_detector import policy_proposer, risk_scorer
from part2.drift_detector.role_analyzer import analyze
from shared.iam_role_client import list_managed_roles
from shared.logging_config import get_logger, new_correlation_id

log = get_logger(__name__)

AUDIT_TABLE_ENV = "ZEROSHIFT_SCP_AUDIT_TABLE"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    correlation_id = new_correlation_id()
    body = _extract_body(event)
    lookback_days = int(body.get("lookbackDays") or 90)
    role_arn_filter = body.get("roleArn")

    log.info(
        "drift_scan_start",
        extra={"lookbackDays": lookback_days, "roleArnFilter": role_arn_filter},
    )

    roles = list_managed_roles()
    if role_arn_filter:
        roles = [r for r in roles if r.role_arn == role_arn_filter]

    findings = []
    for role in roles:
        analysis = analyze(role, lookback_days=lookback_days)
        # Detect whether the drift includes wildcard scope reduction.
        wildcard_scope_reduction = any(
            "*" in a.split(":", 1)[-1] for a in analysis.actions_to_remove
        )
        tier = risk_scorer.score(
            environment=role.environment,
            removed_actions=analysis.actions_to_remove,
            is_wildcard_scope_reduction=wildcard_scope_reduction,
        )
        proposal = policy_proposer.propose(
            role_arn=role.role_arn,
            original_policy=role.attached_policy_document,
            actions_to_remove=analysis.actions_to_remove,
        )

        per_role = {
            "roleArn": role.role_arn,
            "roleName": role.role_name,
            "environment": role.environment,
            "attachedPolicyId": role.attached_policy_id,
            "attachedPolicyArn": role.attached_policy_arn,
            "isRealRole": role.is_real_role,
            "riskTier": tier,
            "actionsToRemove": analysis.actions_to_remove,
            "findings": [asdict(f) for f in analysis.findings],
            "proposal": proposal,
        }
        findings.append(per_role)
        _persist(per_role, correlation_id=correlation_id)

    log.info("drift_scan_done", extra={"roleCount": len(findings)})

    return _api_response(
        {
            "correlationId": correlation_id,
            "lookbackDays": lookback_days,
            "roleCount": len(findings),
            "findings": findings,
        }
    )


def _persist(per_role: dict[str, Any], *, correlation_id: str) -> None:
    if not per_role.get("actionsToRemove"):
        return
    table_name = os.environ.get(AUDIT_TABLE_ENV)
    if not table_name:
        return
    table = boto3.resource("dynamodb").Table(table_name)
    now = datetime.now(timezone.utc).isoformat()
    change_id = str(uuid.uuid4())
    item = {
        "scpId": per_role["roleArn"],
        "changeId#timestamp": f"{change_id}#{now}",
        "changeType": "DRIFT_PROPOSAL",
        "roleName": per_role.get("roleName"),
        "riskTier": per_role.get("riskTier"),
        "environment": per_role.get("environment"),
        "actionsToRemove": per_role.get("actionsToRemove"),
        "correlationId": correlation_id,
        "createdAt": now,
    }
    item = {k: v for k, v in item.items() if v is not None}
    table.put_item(Item=item)


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
