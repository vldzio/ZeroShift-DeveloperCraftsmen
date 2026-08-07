"""Access helpers for the two ZeroShift DynamoDB tables.

- ``zeroshift-intent-registry``: shared across Parts 1 and 2 (Part 3 does not
  write to it at v1, but the helpers exist so Parts 1/2 have zero-friction
  integration when they land).
- ``zeroshift-scp-audit``: Part 3's own audit trail (denial analyses,
  refactoring proposals, approver records).

Table names are read from environment variables set by the CDK stacks.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

from .logging_config import get_logger

log = get_logger(__name__)

INTENT_TABLE_ENV = "ZEROSHIFT_INTENT_TABLE"
AUDIT_TABLE_ENV = "ZEROSHIFT_SCP_AUDIT_TABLE"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table(env_name: str):
    name = os.environ.get(env_name)
    if not name:
        raise RuntimeError(f"Environment variable {env_name} is not set")
    return boto3.resource("dynamodb").Table(name)


# ---- Intent registry (Parts 1 & 2, reserved for Part 3 use later) ----

def put_intent(
    role_arn: str,
    permission: str,
    resource: str,
    status: str,
    *,
    added_at_commit: str | None = None,
    removed_at_commit: str | None = None,
    grace_period_expiry: str | None = None,
    risk_score: str = "LOW",
    source: str = "PART3_DRIFT_DETECTION",
    version: int = 1,
) -> dict[str, Any]:
    """Write an intent record. Matches the schema from zeroshift.md."""
    item = {
        "roleArn": role_arn,
        "permission#version": f"{permission}#{version}",
        "permission": permission,
        "resource": resource,
        "status": status,
        "addedAtCommit": added_at_commit,
        "removedAtCommit": removed_at_commit,
        "gracePeriodExpiry": grace_period_expiry,
        "riskScore": risk_score,
        "source": source,
        "lastUpdated": _now_iso(),
    }
    item = {k: v for k, v in item.items() if v is not None}
    _table(INTENT_TABLE_ENV).put_item(Item=item)
    return item


def get_active_permissions(role_arn: str) -> list[dict[str, Any]]:
    resp = _table(INTENT_TABLE_ENV).query(
        KeyConditionExpression="roleArn = :ra",
        ExpressionAttributeValues={":ra": role_arn},
    )
    return [item for item in resp.get("Items", []) if item.get("status") == "ACTIVE"]


# ---- SCP audit trail (Part 3) ----

def put_denial_analysis(
    *,
    scp_id: str,
    scp_name: str,
    statement_id: str | None,
    action: str,
    resource: str,
    principal: str,
    account_id: str,
    explanation: dict[str, Any],
    correlation_id: str,
    input_event_reference: dict[str, Any] | None = None,
    ct_managed: bool = False,
) -> dict[str, Any]:
    """Write a denial-analysis record to the SCP audit table."""
    now = _now_iso()
    change_id = str(uuid.uuid4())
    item = {
        "scpId": scp_id,
        "changeId#timestamp": f"{change_id}#{now}",
        "changeType": "DENIAL_ANALYSIS",
        "scpName": scp_name,
        "statementId": statement_id,
        "action": action,
        "resource": resource,
        "principal": principal,
        "accountId": account_id,
        "explanation": explanation,
        "controlTowerManaged": ct_managed,
        "correlationId": correlation_id,
        "inputEventReference": input_event_reference,
        "createdAt": now,
    }
    item = {k: v for k, v in item.items() if v is not None}
    _table(AUDIT_TABLE_ENV).put_item(Item=item)
    return item


def get_audit_for_scp(scp_id: str, limit: int = 25) -> list[dict[str, Any]]:
    resp = _table(AUDIT_TABLE_ENV).query(
        KeyConditionExpression="scpId = :s",
        ExpressionAttributeValues={":s": scp_id},
        Limit=limit,
        ScanIndexForward=False,
    )
    return resp.get("Items", [])
