"""Diff the analyzed SDK calls against the intent registry and produce deltas.

For a given target role:
- Any inferred action NOT already in the intent registry becomes a proposed
  ACTIVE write (Part 1's contribution: "the code needs this now").
- Any intent-registry ACTIVE action NOT in the inferred set becomes a proposed
  PENDING_REMOVAL with a grace-period expiry (Part 1's contribution: "the code
  no longer needs this; Part 2 can execute the removal after the grace").

The writer can either return the deltas without persisting (dry-run) or
actually write to DynamoDB.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from shared.dynamodb_helpers import get_active_permissions, put_intent
from shared.logging_config import get_logger

log = get_logger(__name__)

DEFAULT_GRACE_DAYS = 7


@dataclass
class IntentDelta:
    action: str
    change: str  # "ADD_ACTIVE" | "MARK_PENDING_REMOVAL" | "UNCHANGED_ACTIVE"


def diff(
    *,
    role_arn: str,
    inferred_actions: list[str],
) -> dict[str, Any]:
    """Compute deltas without writing to DynamoDB.

    Reads the current intent registry for the role and compares against
    ``inferred_actions``. Returns three lists: to_add, to_mark_pending,
    unchanged.
    """
    inferred_set = set(inferred_actions)
    try:
        current_items = get_active_permissions(role_arn)
    except Exception as exc:
        log.warning("intent_registry_read_failed", extra={"roleArn": role_arn, "error": str(exc)})
        current_items = []
    current_active = {item["permission"] for item in current_items if item.get("status") == "ACTIVE"}

    to_add = sorted(inferred_set - current_active)
    to_mark_pending = sorted(current_active - inferred_set)
    unchanged = sorted(inferred_set & current_active)

    return {
        "roleArn": role_arn,
        "inferredActions": sorted(inferred_set),
        "currentActive": sorted(current_active),
        "toAdd": to_add,
        "toMarkPendingRemoval": to_mark_pending,
        "unchanged": unchanged,
    }


def apply_diff(
    *,
    role_arn: str,
    delta: dict[str, Any],
    commit_hash: str | None = None,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> dict[str, Any]:
    """Persist the delta to the intent registry.

    ACTIVE rows are written for newly-inferred actions. PENDING_REMOVAL rows
    are written for actions the code no longer uses, with a grace-period
    expiry ``grace_days`` in the future. Existing ACTIVE rows for
    still-inferred actions are left untouched.
    """
    grace_expiry = (datetime.now(timezone.utc) + timedelta(days=grace_days)).isoformat()
    added = 0
    marked = 0

    for action in delta.get("toAdd", []) or []:
        try:
            put_intent(
                role_arn=role_arn,
                permission=action,
                resource="*",
                status="ACTIVE",
                added_at_commit=commit_hash or "part1-manual",
                risk_score="LOW",
                source="PART1_CODE_ANALYSIS",
                version=1,
            )
            added += 1
        except Exception as exc:
            log.warning("intent_add_failed", extra={"action": action, "error": str(exc)})

    for action in delta.get("toMarkPendingRemoval", []) or []:
        try:
            put_intent(
                role_arn=role_arn,
                permission=action,
                resource="*",
                status="PENDING_REMOVAL",
                added_at_commit=None,
                removed_at_commit=commit_hash or "part1-manual",
                grace_period_expiry=grace_expiry,
                risk_score="LOW",
                source="PART1_CODE_ANALYSIS",
                version=1,
            )
            marked += 1
        except Exception as exc:
            log.warning("intent_mark_pending_failed", extra={"action": action, "error": str(exc)})

    return {
        "gracePeriodExpiry": grace_expiry,
        "activeAdded": added,
        "pendingRemovalMarked": marked,
    }
