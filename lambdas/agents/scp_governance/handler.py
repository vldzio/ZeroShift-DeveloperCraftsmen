"""Lambda entry point for the SCP Governance Agent."""
from __future__ import annotations

import os
from typing import Any

from agents.common.reasoning_logger import ReasoningLogger
from shared.logging_config import get_logger, new_correlation_id

log = get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    correlation_id = event.get("correlationId") or new_correlation_id()

    scp_id = event.get("scpId")
    scp_name = event.get("scpName") or ""
    original_document = event.get("originalDocument") or event.get("document") or {}
    proposed_document = (event.get("proposal") or {}).get("compressed_document") or event.get("proposedDocument") or {}
    equivalence_report = event.get("equivalence") or {}

    log.info(
        "scp_agent_start",
        extra={"scpId": scp_id, "correlationId": correlation_id, "scpName": scp_name},
    )

    if not scp_id or not proposed_document:
        return _fallback("scpId and proposedDocument are required", correlation_id=correlation_id)

    logger = ReasoningLogger(agent_name="scp_governance", correlation_id=correlation_id)

    try:
        from agents.scp_governance.graph import build_graph

        app = build_graph()
        initial: dict[str, Any] = {
            "scp_id": scp_id,
            "scp_name": scp_name,
            "original_document": original_document,
            "proposed_document": proposed_document,
            "equivalence_report": equivalence_report,
            "correlation_id": correlation_id,
            "reasoning_logger": logger,
        }
        final = app.invoke(initial)
        return {
            "applyResult": {
                "applied": final.get("outcome") == "success",
                "outcome": final.get("outcome"),
                "reason": "agentic",
                "scpId": scp_id,
                "scpName": scp_name,
                "rollbackReason": final.get("rollback_reason"),
                "agentExecutionId": logger.agent_execution_id,
                "reasoningTable": os.environ.get("ZEROSHIFT_AGENT_REASONING_TABLE"),
            },
            "correlationId": correlation_id,
            "fallback": False,
        }
    except Exception as exc:
        log.warning("scp_agent_exception", extra={"error": str(exc)})
        return _fallback(f"agent exception: {exc}", correlation_id=correlation_id, agent_execution_id=logger.agent_execution_id)


def _fallback(reason: str, *, correlation_id: str = "", agent_execution_id: str = "") -> dict[str, Any]:
    return {
        "fallback": True,
        "reason": reason,
        "correlationId": correlation_id,
        "agentExecutionId": agent_execution_id,
        "applyResult": None,
    }
