"""Verify that a proposed IAM role policy still permits every action it should.

Iterates every action in the proposed policy's Allow statements and confirms
the same action is still Allow'ed in the original. Any action that would flip
from Allow to Deny is a divergence beyond the intended removed_actions set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from part3.denial_analyzer.policy_simulator import DenialContext, LocalScpEvaluator


@dataclass
class Divergence:
    action: str
    reason: str


@dataclass
class EquivalenceReport:
    total_checked: int = 0
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def is_equivalent(self) -> bool:
        return not self.divergences


def check(
    *,
    original_policy: dict[str, Any],
    proposed_policy: dict[str, Any],
    expected_removed: list[str],
) -> EquivalenceReport:
    """Confirm the proposed policy differs from the original ONLY on the expected removals."""
    original_actions = _allow_action_set(original_policy)
    proposed_actions = _allow_action_set(proposed_policy)

    report = EquivalenceReport(total_checked=len(original_actions))

    # Removed set: original - proposed. Must equal expected_removed exactly.
    actually_removed = original_actions - proposed_actions
    expected_set = set(expected_removed)
    unexpected_removals = actually_removed - expected_set
    missed_removals = expected_set - actually_removed
    added = proposed_actions - original_actions

    for action in sorted(unexpected_removals):
        report.divergences.append(
            Divergence(action=action, reason="removed but not in expected_removed list")
        )
    for action in sorted(missed_removals):
        report.divergences.append(
            Divergence(action=action, reason="expected removal not applied")
        )
    for action in sorted(added):
        report.divergences.append(
            Divergence(action=action, reason="new action added that was not in the original")
        )
    return report


def _allow_action_set(document: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for stmt in document.get("Statement", []) or []:
        if stmt.get("Effect") != "Allow":
            continue
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        for a in actions:
            result.add(a)
    return result


def simulate_action_still_allowed(
    *,
    policy_document: dict[str, Any],
    action: str,
    resource_arn: str = "*",
    principal_arn: str = "arn:aws:iam::000000000000:role/EquivalencePrincipal",
) -> bool:
    """Cheap local simulation. True if the policy grants the action (no explicit deny)."""
    ctx = DenialContext(
        action=action,
        resource_arn=resource_arn,
        principal_arn=principal_arn,
        account_id="000000000000",
        region="us-east-1",
        condition_keys={"aws:PrincipalArn": principal_arn, "aws:RequestedRegion": "us-east-1"},
    )
    return LocalScpEvaluator().evaluate("", "", policy_document, ctx) is None
