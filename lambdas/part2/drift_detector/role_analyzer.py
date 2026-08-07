"""Per-role IAM drift analysis.

Cross-references the role's attached policy against CloudTrail activity and the
Part 1 intent registry to produce the set of "drifted" (unused or pending-removal)
permissions. The registry entries mean:

- ``status=ACTIVE`` with a recent commit -> intentional; preserve even if unused
- ``status=PENDING_REMOVAL`` with expired grace period -> remove
- ``status=PENDING_REMOVAL`` with active grace period -> preserve until expiry
- No entry -> use CloudTrail signal alone
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import boto3

from shared.cloudtrail_lake_client import count_activity
from shared.iam_role_client import ManagedRole
from shared.logging_config import get_logger

log = get_logger(__name__)

INTENT_TABLE_ENV = "ZEROSHIFT_INTENT_TABLE"


@dataclass
class DriftFinding:
    action: str
    resource: str
    activity_count: int
    source: str  # "UNUSED_CLOUDTRAIL" | "PENDING_REMOVAL_EXPIRED" | "PENDING_REMOVAL_ACTIVE_PRESERVED" | "INTENT_ACTIVE_PRESERVED"
    should_remove: bool


@dataclass
class RoleAnalysis:
    role: ManagedRole
    scanned_actions: list[str]
    activity_counts: dict[str, int]
    intent_entries: list[dict[str, Any]] = field(default_factory=list)
    findings: list[DriftFinding] = field(default_factory=list)
    actions_to_remove: list[str] = field(default_factory=list)


def _all_policy_actions(document: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``[(action, resource)]`` pairs referenced by every Allow statement."""
    pairs: list[tuple[str, str]] = []
    for stmt in document.get("Statement", []) or []:
        if stmt.get("Effect") != "Allow":
            continue
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        resources = stmt.get("Resource", "*")
        if isinstance(resources, str):
            resources = [resources]
        resource_str = ", ".join(resources) if resources else "*"
        for a in actions:
            pairs.append((a, resource_str))
    return pairs


def _load_intent_entries(role_arn: str) -> list[dict[str, Any]]:
    table_name = os.environ.get(INTENT_TABLE_ENV)
    if not table_name:
        return []
    table = boto3.resource("dynamodb").Table(table_name)
    resp = table.query(
        KeyConditionExpression="roleArn = :ra",
        ExpressionAttributeValues={":ra": role_arn},
    )
    return resp.get("Items", []) or []


def _grace_expired(entry: dict[str, Any]) -> bool:
    expiry = entry.get("gracePeriodExpiry")
    if not expiry:
        return True  # No grace period -> treat as expired.
    try:
        return datetime.fromisoformat(expiry.replace("Z", "+00:00")) < datetime.now(timezone.utc)
    except ValueError:
        return True


def analyze(role: ManagedRole, *, lookback_days: int = 90) -> RoleAnalysis:
    """Return drift analysis for a single managed role."""
    pairs = _all_policy_actions(role.attached_policy_document)
    actions = sorted({a for a, _ in pairs})
    resource_lookup: dict[str, str] = {}
    for a, r in pairs:
        resource_lookup.setdefault(a, r)

    counts = count_activity(actions, lookback_days=lookback_days) if actions else {}
    intent_entries = _load_intent_entries(role.role_arn)
    intent_by_permission = {e.get("permission"): e for e in intent_entries}

    findings: list[DriftFinding] = []
    to_remove: list[str] = []

    for action in actions:
        cnt = counts.get(action, 0)
        entry = intent_by_permission.get(action)
        resource = resource_lookup.get(action, "*")

        # Intent registry says PENDING_REMOVAL.
        if entry and entry.get("status") == "PENDING_REMOVAL":
            if _grace_expired(entry):
                findings.append(
                    DriftFinding(
                        action=action,
                        resource=resource,
                        activity_count=cnt,
                        source="PENDING_REMOVAL_EXPIRED",
                        should_remove=True,
                    )
                )
                to_remove.append(action)
            else:
                findings.append(
                    DriftFinding(
                        action=action,
                        resource=resource,
                        activity_count=cnt,
                        source="PENDING_REMOVAL_ACTIVE_PRESERVED",
                        should_remove=False,
                    )
                )
            continue

        # Intent registry says ACTIVE (intentional addition by Part 1).
        if entry and entry.get("status") == "ACTIVE":
            findings.append(
                DriftFinding(
                    action=action,
                    resource=resource,
                    activity_count=cnt,
                    source="INTENT_ACTIVE_PRESERVED",
                    should_remove=False,
                )
            )
            continue

        # Otherwise: rely on CloudTrail signal alone.
        if cnt == 0:
            findings.append(
                DriftFinding(
                    action=action,
                    resource=resource,
                    activity_count=0,
                    source="UNUSED_CLOUDTRAIL",
                    should_remove=True,
                )
            )
            to_remove.append(action)

    return RoleAnalysis(
        role=role,
        scanned_actions=actions,
        activity_counts=counts,
        intent_entries=intent_entries,
        findings=findings,
        actions_to_remove=sorted(set(to_remove)),
    )
