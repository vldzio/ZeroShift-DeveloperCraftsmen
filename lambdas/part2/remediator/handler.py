"""Step Functions task-handler entry points for the IAM Drift Remediation workflow.

The state machine invokes this Lambda for each task via a ``task`` key in the
event. This mirrors the pattern from Part 3 Component 2's refactorer.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import boto3

from part2.drift_detector import policy_proposer, risk_scorer
from part2.drift_detector.role_analyzer import analyze
from part2.remediator import apply_policy
from part2.remediator.equivalence_checker import check as check_equivalence
from shared.dynamodb_helpers import put_intent
from shared.iam_role_client import get_role_by_arn
from shared.logging_config import get_logger, new_correlation_id

log = get_logger(__name__)

AUDIT_TABLE_ENV = "ZEROSHIFT_SCP_AUDIT_TABLE"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    task = event.get("task")
    if not task:
        raise ValueError("event.task is required")
    correlation_id = event.get("correlationId") or new_correlation_id()

    dispatch = {
        "load_role": _load_role,
        "check_managed": _check_managed,
        "scan_usage": _scan_usage,
        "read_intent_registry": _read_intent_registry,
        "propose_replacement": _propose_replacement,
        "simulate_equivalence": _simulate_equivalence,
        "score_risk": _score_risk,
        "apply_policy": _apply_policy,
        "update_intent_registry": _update_intent_registry,
        "record_approval_required": _record_approval_required,
    }
    if task not in dispatch:
        raise ValueError(f"Unknown task: {task}")

    result = dispatch[task](event)
    result["correlationId"] = correlation_id
    result["task"] = task
    return result


# ---- Tasks ----

def _load_role(event: dict[str, Any]) -> dict[str, Any]:
    role_arn = event["roleArn"]
    role = get_role_by_arn(role_arn)
    if not role:
        raise ValueError(f"Unknown role: {role_arn}")
    return {
        "roleArn": role.role_arn,
        "roleName": role.role_name,
        "environment": role.environment,
        "attachedPolicyId": role.attached_policy_id,
        "attachedPolicyArn": role.attached_policy_arn,
        "isRealRole": role.is_real_role,
        "originalPolicy": role.attached_policy_document,
        "tags": role.tags,
    }


def _check_managed(event: dict[str, Any]) -> dict[str, Any]:
    tags = event.get("tags") or {}
    is_managed = tags.get("ManagedBy") == "ZeroShift"
    return {"isManaged": is_managed, "shortCircuit": not is_managed}


def _scan_usage(event: dict[str, Any]) -> dict[str, Any]:
    role = get_role_by_arn(event["roleArn"])
    if not role:
        raise ValueError(f"Unknown role: {event['roleArn']}")
    analysis = analyze(role, lookback_days=int(event.get("lookbackDays") or 90))
    return {
        "scannedActions": analysis.scanned_actions,
        "activityCounts": analysis.activity_counts,
        "findings": [asdict(f) for f in analysis.findings],
        "actionsToRemove": analysis.actions_to_remove,
    }


def _read_intent_registry(event: dict[str, Any]) -> dict[str, Any]:
    # role_analyzer.analyze already reads the registry; this task exists as a
    # discrete state machine step for observability. Return the intent entries
    # separately for the frontend timeline detail line.
    role = get_role_by_arn(event["roleArn"])
    if not role:
        return {"intentEntries": []}
    analysis = analyze(role, lookback_days=int(event.get("lookbackDays") or 90))
    return {"intentEntries": analysis.intent_entries or []}


def _propose_replacement(event: dict[str, Any]) -> dict[str, Any]:
    proposal = policy_proposer.propose(
        role_arn=event["roleArn"],
        original_policy=event["originalPolicy"],
        actions_to_remove=event.get("actionsToRemove", []),
    )
    return {"proposal": proposal}


def _simulate_equivalence(event: dict[str, Any]) -> dict[str, Any]:
    proposed_policy = (event.get("proposal") or {}).get("proposed_policy") or {}
    report = check_equivalence(
        original_policy=event["originalPolicy"],
        proposed_policy=proposed_policy,
        expected_removed=event.get("actionsToRemove", []),
    )
    return {
        "equivalence": {
            "isEquivalent": report.is_equivalent,
            "totalChecked": report.total_checked,
            "divergences": [asdict(d) for d in report.divergences],
        }
    }


def _score_risk(event: dict[str, Any]) -> dict[str, Any]:
    removed = event.get("actionsToRemove", [])
    tier = risk_scorer.score(
        environment=event.get("environment"),
        removed_actions=removed,
        is_wildcard_scope_reduction=any("*" in a.split(":", 1)[-1] for a in removed),
    )
    return {"riskTier": tier}


def _apply_policy(event: dict[str, Any]) -> dict[str, Any]:
    proposed_policy = (event.get("proposal") or {}).get("proposed_policy") or {}
    result = apply_policy.apply(role_arn=event["roleArn"], proposed_policy=proposed_policy)
    return {"applyResult": result}


def _update_intent_registry(event: dict[str, Any]) -> dict[str, Any]:
    role_arn = event["roleArn"]
    removed = event.get("actionsToRemove", [])
    now = datetime.now(timezone.utc).isoformat()
    for action in removed:
        try:
            put_intent(
                role_arn=role_arn,
                permission=action,
                resource="*",
                status="REMOVED",
                removed_at_commit=f"part2-drift-{event.get('correlationId', 'unknown')}",
                risk_score=event.get("riskTier", "LOW"),
                source="PART2_DRIFT_DETECTION",
                version=2,
            )
        except Exception as exc:
            log.warning("intent_registry_write_failed", extra={"action": action, "error": str(exc)})

    table_name = os.environ.get(AUDIT_TABLE_ENV)
    if table_name:
        boto3.resource("dynamodb").Table(table_name).put_item(
            Item={
                "scpId": role_arn,
                "changeId#timestamp": f"{uuid.uuid4()}#{now}",
                "changeType": "DRIFT_REMEDIATION_APPLIED",
                "riskTier": event.get("riskTier"),
                "actionsRemoved": removed,
                "correlationId": event.get("correlationId"),
                "createdAt": now,
                "applyResult": (event.get("applyResult") or {}),
            }
        )
    return {"registryUpdated": True, "removedCount": len(removed)}


def _record_approval_required(event: dict[str, Any]) -> dict[str, Any]:
    """Terminal state for HIGH-tier proposals — write an approval-required audit row."""
    role_arn = event["roleArn"]
    now = datetime.now(timezone.utc).isoformat()
    table_name = os.environ.get(AUDIT_TABLE_ENV)
    if table_name:
        boto3.resource("dynamodb").Table(table_name).put_item(
            Item={
                "scpId": role_arn,
                "changeId#timestamp": f"{uuid.uuid4()}#{now}",
                "changeType": "DRIFT_APPROVAL_REQUIRED",
                "riskTier": event.get("riskTier"),
                "actionsToRemove": event.get("actionsToRemove", []),
                "correlationId": event.get("correlationId"),
                "createdAt": now,
                "note": "Safe-mode v1: HIGH-tier drift held for human approval; policy NOT mutated.",
            }
        )
    return {"approvalRequired": True}
