"""Per-statement staleness evaluation.

A Deny statement is considered a dead-deny if none of its enumerated actions
have been attempted in the CloudTrail lookback window.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from shared.iam_actions import all_actions, expand_action_pattern


@dataclass
class StatementFinding:
    scp_id: str
    scp_name: str
    statement_sid: str | None
    statement_effect: str
    actions: list[str]
    action_counts: dict[str, int]
    total_activity: int
    lookback_days: int
    recommendation: str
    is_stale: bool = field(default=False)


def enumerate_statement_actions(statement: dict[str, Any]) -> list[str]:
    """Return concrete IAM action strings covered by this statement.

    Only actions are enumerated here — resources and conditions are ignored for
    the staleness check. If the statement omits both Action and NotAction it is
    treated as covering all actions (matches AWS SCP semantics).
    """
    if "Action" in statement:
        result: set[str] = set()
        patterns = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        for pattern in patterns:
            for action in expand_action_pattern(pattern):
                result.add(action)
        return sorted(result)
    if "NotAction" in statement:
        patterns = statement["NotAction"] if isinstance(statement["NotAction"], list) else [statement["NotAction"]]
        excluded: set[str] = set()
        for pattern in patterns:
            for action in expand_action_pattern(pattern):
                excluded.add(action)
        return sorted(a for a in all_actions() if a not in excluded and not _matches_any(a, patterns))
    return sorted(all_actions())


def _matches_any(action: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if pat == "*" or fnmatch.fnmatchcase(action, pat):
            return True
    return False


def analyze_statement(
    *,
    scp_id: str,
    scp_name: str,
    statement: dict[str, Any],
    action_counts: dict[str, int],
    lookback_days: int,
) -> StatementFinding:
    actions = enumerate_statement_actions(statement)
    counts_for_stmt = {a: int(action_counts.get(a, 0)) for a in actions}
    total = sum(counts_for_stmt.values())
    is_stale = statement.get("Effect") == "Deny" and len(actions) > 0 and total == 0

    if is_stale:
        recommendation = (
            f"No CloudTrail activity in the last {lookback_days} days for any of the "
            f"{len(actions)} action(s) this Deny statement covers. Consider removing "
            "the statement or narrowing it to the actions actually being attempted."
        )
    else:
        recommendation = ""

    return StatementFinding(
        scp_id=scp_id,
        scp_name=scp_name,
        statement_sid=statement.get("Sid"),
        statement_effect=statement.get("Effect", "Unknown"),
        actions=actions,
        action_counts=counts_for_stmt,
        total_activity=total,
        lookback_days=lookback_days,
        recommendation=recommendation,
        is_stale=is_stale,
    )
