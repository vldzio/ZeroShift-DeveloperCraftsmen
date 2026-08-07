"""Persists LangGraph agent decision-log rows to the reasoning DynamoDB table.

Table shape:
  Partition key: agentExecutionId (UUID per agent invocation)
  Sort key:      stepId#timestamp  (increments per node visit)

Every row captures one node visit — enough for the frontend to render a live
trace of the agent's reasoning during a remediation.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import boto3

from shared.logging_config import get_logger

log = get_logger(__name__)

REASONING_TABLE_ENV = "ZEROSHIFT_AGENT_REASONING_TABLE"


@dataclass
class ReasoningLogger:
    agent_execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_name: str = ""
    correlation_id: str = ""
    _step_index: int = 0

    def log_step(
        self,
        *,
        node: str,
        summary: str,
        llm_output: Any = None,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result: dict[str, Any] | None = None,
        next_node: str | None = None,
    ) -> dict[str, Any]:
        self._step_index += 1
        now = datetime.now(timezone.utc).isoformat()
        step_id = f"{self._step_index:04d}"
        item = {
            "agentExecutionId": self.agent_execution_id,
            "stepId#timestamp": f"{step_id}#{now}",
            "stepIndex": self._step_index,
            "agentName": self.agent_name,
            "correlationId": self.correlation_id,
            "node": node,
            "summary": summary,
            "toolName": tool_name,
            "toolArgs": _redact(tool_args) if tool_args else None,
            "toolResult": _truncate(tool_result) if tool_result else None,
            "llmOutput": _truncate({"text": str(llm_output)}) if llm_output else None,
            "nextNode": next_node,
            "createdAt": now,
        }
        item = {k: v for k, v in item.items() if v is not None}
        table_name = os.environ.get(REASONING_TABLE_ENV)
        if not table_name:
            log.info("reasoning_log_no_table", extra={"item": item})
            return item
        try:
            boto3.resource("dynamodb").Table(table_name).put_item(Item=item)
        except Exception as exc:
            log.warning("reasoning_log_write_failed", extra={"error": str(exc)})
        return item


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Truncate huge policy documents in tool args so DynamoDB rows stay small."""
    return _truncate(payload)


def _truncate(payload: dict[str, Any], max_bytes: int = 12_000) -> dict[str, Any]:
    import json

    encoded = json.dumps(payload, default=str)
    if len(encoded) <= max_bytes:
        return payload
    return {"_truncated": True, "_originalBytes": len(encoded), "_preview": encoded[:max_bytes]}
