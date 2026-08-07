"""Deterministic SCP equivalence check.

Given an original SCP document and a proposed compressed replacement, iterate
every concrete IAM action covered by the original (via the curated action
catalog) and confirm the two documents produce identical decisions for every
action.

Uses ``LocalScpEvaluator`` when ``ZEROSHIFT_FIXTURE_MODE=true``, otherwise
calls ``iam:SimulateCustomPolicy``. Either way the safety guarantee is the
same: if any action produces a different decision between the two documents,
equivalence fails and the diverging action(s) are reported.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import boto3

from part3.denial_analyzer.policy_simulator import DenialContext, LocalScpEvaluator
from shared.iam_actions import list_all_actions_for_scp
from shared.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class Divergence:
    action: str
    original_decision: str
    proposed_decision: str


@dataclass
class EquivalenceReport:
    total_actions_checked: int
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def is_equivalent(self) -> bool:
        return not self.divergences


def check(*, original: dict[str, Any], proposed: dict[str, Any], scp_id: str) -> EquivalenceReport:
    """Enumerate actions in the original SCP and confirm both documents agree."""
    actions = list_all_actions_for_scp(original)
    log.info(
        "equivalence_check_start",
        extra={"scpId": scp_id, "actionCount": len(actions)},
    )

    use_aws = os.environ.get("ZEROSHIFT_FIXTURE_MODE", "true").lower() != "true"
    evaluator = _AwsEvaluator() if use_aws else _LocalEvaluator()

    report = EquivalenceReport(total_actions_checked=len(actions))
    for action in actions:
        original_decision = evaluator.decide(original, action)
        proposed_decision = evaluator.decide(proposed, action)
        if original_decision != proposed_decision:
            report.divergences.append(
                Divergence(action=action, original_decision=original_decision, proposed_decision=proposed_decision)
            )

    log.info(
        "equivalence_check_done",
        extra={"scpId": scp_id, "isEquivalent": report.is_equivalent, "divergenceCount": len(report.divergences)},
    )
    return report


class _LocalEvaluator:
    def __init__(self) -> None:
        self._sim = LocalScpEvaluator()

    def decide(self, document: dict[str, Any], action: str) -> str:
        context = DenialContext(
            action=action,
            resource_arn="*",
            principal_arn="arn:aws:iam::111111111111:role/EquivalenceCheckPrincipal",
            account_id="111111111111",
            region="us-east-1",
            condition_keys={
                "aws:PrincipalTag/Team": "developers",
                "aws:RequestedRegion": "us-east-1",
            },
        )
        decision = self._sim.evaluate(scp_id="orig-or-proposed", scp_name="", document=document, context=context)
        return "explicitDeny" if decision is not None else "allowed"


class _AwsEvaluator:
    def __init__(self) -> None:
        self._iam = boto3.client("iam")

    def decide(self, document: dict[str, Any], action: str) -> str:
        resp = self._iam.simulate_custom_policy(
            PolicyInputList=[json.dumps(document)],
            ActionNames=[action],
            ResourceArns=["*"],
        )
        results = resp.get("EvaluationResults", [])
        if not results:
            return "unknown"
        return results[0].get("EvalDecision", "unknown")
