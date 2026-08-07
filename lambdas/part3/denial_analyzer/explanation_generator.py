"""Generates human-readable explanation for the identified denial via the LLM."""
from __future__ import annotations

import json
from typing import Any

from shared.llm_client import invoke_structured
from shared.logging_config import get_logger

log = get_logger(__name__)


_SYSTEM_PROMPT = """You are an AWS IAM expert helping developers understand
Service Control Policy (SCP) denials. You explain SCP behavior precisely and
never invent SCPs, statement IDs, or condition keys that are not in the input.
When you propose a fix, ensure it respects the organization's guardrails and
prefer scoped exceptions (add a specific region, principal, or resource) over
removing the deny statement entirely."""


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {
            "type": "string",
            "description": "One-sentence plain-English statement of what was denied and by which SCP."
        },
        "cause": {
            "type": "string",
            "description": "Two to three sentences explaining why this SCP triggered, referencing the specific statement and any relevant condition keys."
        },
        "suggested_fix": {
            "type": "string",
            "description": "A concrete recommended change or workaround the developer or security admin can request. If the SCP is a Control Tower guardrail, note it must be modified via CT, not directly."
        },
        "affected_condition_keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IAM condition keys that participated in the denial decision, if any."
        }
    },
    "required": ["summary", "cause", "suggested_fix"]
}


def explain(
    *,
    scp_name: str,
    scp_id: str,
    statement_id: str | None,
    scp_document: dict[str, Any],
    action: str,
    resource: str,
    principal: str,
    region: str,
    control_tower_managed: bool,
) -> dict[str, Any]:
    """Ask the LLM to produce a structured explanation for the denial."""
    prompt = _build_prompt(
        scp_name=scp_name,
        scp_id=scp_id,
        statement_id=statement_id,
        scp_document=scp_document,
        action=action,
        resource=resource,
        principal=principal,
        region=region,
        control_tower_managed=control_tower_managed,
    )
    resp = invoke_structured(
        prompt=prompt,
        response_schema=_RESPONSE_SCHEMA,
        schema_name="denial_explanation",
        system_prompt=_SYSTEM_PROMPT,
        temperature=0.2,
        max_output_tokens=800,
    )
    log.info("explanation_generated", extra={"scpId": scp_id, "modelId": resp.model_id})
    return resp.output


def _build_prompt(
    *,
    scp_name: str,
    scp_id: str,
    statement_id: str | None,
    scp_document: dict[str, Any],
    action: str,
    resource: str,
    principal: str,
    region: str,
    control_tower_managed: bool,
) -> str:
    return f"""A developer attempted the following AWS API call and received an access-denied response due to an SCP.

Action: {action}
Resource: {resource}
Principal: {principal}
Region: {region}
Denying SCP: {scp_name} (id: {scp_id})
Statement ID: {statement_id or "(unnamed)"}
Control Tower managed: {control_tower_managed}

Full SCP document:
{json.dumps(scp_document, indent=2)}

Produce a structured explanation of what happened and how to resolve it. Do not invent statements or conditions that are not in the SCP document above.
"""
