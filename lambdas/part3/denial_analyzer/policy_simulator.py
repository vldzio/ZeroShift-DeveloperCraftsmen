"""SCP evaluation for denial-attribution.

Two evaluation backends:

- ``LocalScpEvaluator``: a minimal SCP semantics evaluator implemented in pure
  Python. Deterministic, dependency-free, works in unit tests and in fixture
  mode. Handles Effect/Action/NotAction/Resource/Condition for the operators
  used by ZeroShift fixtures (StringEquals, StringNotEquals, StringLike).

- ``AwsPolicySimulator``: calls ``iam:SimulateCustomPolicy``. Used when
  ``ZEROSHIFT_FIXTURE_MODE=false`` for real-world attribution.

The dispatcher ``evaluate(scp, context)`` picks the backend based on the
environment.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import Any

import boto3


@dataclass
class DenialContext:
    """The request context extracted from a CloudTrail access-denied event."""

    action: str
    resource_arn: str
    principal_arn: str
    account_id: str
    region: str
    condition_keys: dict[str, str]


@dataclass
class StatementDecision:
    scp_id: str
    scp_name: str
    statement_id: str | None
    effect: str  # "Deny" | "Allow" | "NotApplicable"
    matched: bool


def evaluate(scp_id: str, scp_name: str, document: dict[str, Any], context: DenialContext) -> StatementDecision | None:
    """Return the first Deny statement in this SCP that matches the context, or None."""
    if _use_aws_simulator():
        return AwsPolicySimulator().evaluate(scp_id, scp_name, document, context)
    return LocalScpEvaluator().evaluate(scp_id, scp_name, document, context)


def _use_aws_simulator() -> bool:
    fixture_mode = os.environ.get("ZEROSHIFT_FIXTURE_MODE", "true").lower() == "true"
    return not fixture_mode


class LocalScpEvaluator:
    """Deterministic SCP evaluator for the operators used in ZeroShift fixtures."""

    def evaluate(
        self,
        scp_id: str,
        scp_name: str,
        document: dict[str, Any],
        context: DenialContext,
    ) -> StatementDecision | None:
        for stmt in document.get("Statement", []):
            if stmt.get("Effect") != "Deny":
                continue
            if not _action_matches(stmt, context.action):
                continue
            if not _resource_matches(stmt.get("Resource", "*"), context.resource_arn):
                continue
            if not _condition_matches(stmt.get("Condition", {}), context):
                continue
            return StatementDecision(
                scp_id=scp_id,
                scp_name=scp_name,
                statement_id=stmt.get("Sid"),
                effect="Deny",
                matched=True,
            )
        return None


def _action_matches(stmt: dict[str, Any], action: str) -> bool:
    """Handle Action (allow-list of actions) and NotAction (deny-list except these)."""
    if "Action" in stmt:
        return _list_glob_match(stmt["Action"], action)
    if "NotAction" in stmt:
        return not _list_glob_match(stmt["NotAction"], action)
    return True  # No Action or NotAction means the statement applies to all actions


def _resource_matches(resource_field: Any, resource_arn: str) -> bool:
    if isinstance(resource_field, str):
        resources = [resource_field]
    else:
        resources = list(resource_field)
    for pattern in resources:
        if fnmatch.fnmatchcase(resource_arn, pattern):
            return True
        # "*" as a bare wildcard should always match.
        if pattern == "*":
            return True
    return False


def _condition_matches(condition: dict[str, Any], context: DenialContext) -> bool:
    """Evaluate the operators used in ZeroShift fixtures. All operators must
    pass for the condition block to be satisfied."""
    if not condition:
        return True
    for operator, kv in condition.items():
        for key, expected in kv.items():
            actual = context.condition_keys.get(key)
            if actual is None:
                # Absent key -> depends on operator. For StringNotEquals we
                # treat absent as "does not equal", so condition passes.
                if operator == "StringNotEquals":
                    continue
                return False
            expected_values = [expected] if isinstance(expected, str) else list(expected)
            if operator == "StringEquals":
                if actual not in expected_values:
                    return False
            elif operator == "StringNotEquals":
                if actual in expected_values:
                    return False
            elif operator == "StringLike":
                if not any(fnmatch.fnmatchcase(actual, pat) for pat in expected_values):
                    return False
            else:
                # Unknown operator: be conservative and refuse the match, so we
                # do not falsely attribute a denial.
                return False
    return True


def _list_glob_match(patterns: Any, value: str) -> bool:
    if isinstance(patterns, str):
        patterns_list = [patterns]
    else:
        patterns_list = list(patterns)
    for pattern in patterns_list:
        if pattern == "*" or fnmatch.fnmatchcase(value, pattern):
            return True
    return False


class AwsPolicySimulator:
    """Uses IAM ``simulate-custom-policy`` to evaluate a single SCP."""

    def __init__(self) -> None:
        self._iam = boto3.client("iam")

    def evaluate(
        self,
        scp_id: str,
        scp_name: str,
        document: dict[str, Any],
        context: DenialContext,
    ) -> StatementDecision | None:
        import json
        resp = self._iam.simulate_custom_policy(
            PolicyInputList=[json.dumps(document)],
            ActionNames=[context.action],
            ResourceArns=[context.resource_arn] if context.resource_arn else ["*"],
            ContextEntries=self._context_entries(context),
        )
        for result in resp.get("EvaluationResults", []):
            decision = result.get("EvalDecision")
            if decision == "explicitDeny":
                matched = result.get("MatchedStatements") or []
                statement_id = matched[0].get("SourcePolicyId") if matched else None
                return StatementDecision(
                    scp_id=scp_id,
                    scp_name=scp_name,
                    statement_id=statement_id,
                    effect="Deny",
                    matched=True,
                )
        return None

    def _context_entries(self, context: DenialContext) -> list[dict[str, Any]]:
        entries = []
        for key, value in context.condition_keys.items():
            entries.append(
                {
                    "ContextKeyName": key,
                    "ContextKeyValues": [value],
                    "ContextKeyType": "string",
                }
            )
        return entries
