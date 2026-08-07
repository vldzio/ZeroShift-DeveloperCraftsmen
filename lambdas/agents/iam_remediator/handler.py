"""Lambda entry point for the IAM Remediation Agent.

Called by the Part 2 Step Functions state machine's ``AgenticApply`` task.
Loads the LangGraph state graph, runs it, returns the terminal state. On any
exception, returns ``{"fallback": true, "reason": ...}`` so the state machine
can route to the deterministic ``ApplyPolicy`` step.
"""
from __future__ import annotations

import os
from typing import Any

from agents.common.reasoning_logger import ReasoningLogger
from shared.iam_role_client import get_role_by_arn
from shared.logging_config import get_logger, new_correlation_id

log = get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    correlation_id = event.get("correlationId") or new_correlation_id()
    role_arn = event.get("roleArn")
    proposal = (event.get("proposal") or {}).get("proposed_policy") or {}
    original_policy = event.get("originalPolicy") or {}
    actions_to_remove = event.get("actionsToRemove") or []
    risk_tier = event.get("riskTier", "LOW")

    log.info(
        "iam_agent_start",
        extra={"roleArn": role_arn, "correlationId": correlation_id, "riskTier": risk_tier},
    )

    if not role_arn:
        return _fallback("missing roleArn")

    role = get_role_by_arn(role_arn)
    policy_arn = (role.attached_policy_arn if role else None) or event.get("attachedPolicyArn")
    if not policy_arn:
        return _fallback("cannot resolve policy_arn for role")

    logger = ReasoningLogger(
        agent_name="iam_remediator",
        correlation_id=correlation_id,
    )

    try:
        from agents.iam_remediator.graph import build_graph  # heavy import; kept lazy

        app = build_graph()
        initial: dict[str, Any] = {
            "role_arn": role_arn,
            "policy_arn": policy_arn,
            "original_policy": original_policy,
            "proposed_policy": proposal,
            "actions_to_remove": actions_to_remove,
            "risk_tier": risk_tier,
            "correlation_id": correlation_id,
            "reasoning_logger": logger,
        }
        final = app.invoke(initial)
        return {
            "applyResult": {
                "applied": final.get("outcome") == "success",
                "outcome": final.get("outcome"),
                "reason": "agentic",
                "roleArn": role_arn,
                "policyArn": policy_arn,
                "newVersionId": final.get("new_version_id"),
                "rollbackReason": final.get("rollback_reason"),
                "agentExecutionId": logger.agent_execution_id,
                "reasoningTable": os.environ.get("ZEROSHIFT_AGENT_REASONING_TABLE"),
            },
            "correlationId": correlation_id,
            "fallback": False,
        }
    except Exception as exc:
        log.warning("iam_agent_exception", extra={"error": str(exc)})
        return _fallback(f"agent exception: {exc}", correlation_id=correlation_id, agent_execution_id=logger.agent_execution_id)


def _fallback(reason: str, *, correlation_id: str = "", agent_execution_id: str = "") -> dict[str, Any]:
    return {
        "fallback": True,
        "reason": reason,
        "correlationId": correlation_id,
        "agentExecutionId": agent_execution_id,
        "applyResult": None,
    }
