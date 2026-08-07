"""Generate a minimal replacement IAM policy for a role via Kimi K2.5.

The prompt hands the model the original policy plus the concrete list of
actions to remove; Kimi's job is to emit a valid policy that preserves every
other permission exactly.
"""
from __future__ import annotations

import json
from typing import Any

from shared.llm_client import invoke_structured
from shared.logging_config import get_logger

log = get_logger(__name__)

_SYSTEM_PROMPT = """You are an AWS IAM expert scoping down role policies.
You produce minimal replacement policies that remove exactly the listed
unused actions while preserving every other permission the original policy
grants. You do not add new actions. You do not broaden Resource fields.
You do not remove actions that the caller did not ask you to remove.
You preserve Sid, Effect, Resource, and Condition fields on kept statements
verbatim. If removing an action leaves a statement with no Actions, drop
the statement entirely."""

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "proposed_policy": {
            "type": "object",
            "description": "The minimal replacement policy. Valid AWS policy JSON (Version + Statement).",
        },
        "removed_actions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact action strings that were removed. Must be a subset of the caller-supplied list.",
        },
        "rationale": {
            "type": "string",
            "description": "One or two sentences summarizing the change and why the remaining actions are preserved.",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any concerns the model has (e.g. resource ARN interactions with removed actions).",
        },
    },
    "required": ["proposed_policy", "removed_actions", "rationale"],
}


def propose(
    *,
    role_arn: str,
    original_policy: dict[str, Any],
    actions_to_remove: list[str],
) -> dict[str, Any]:
    if not actions_to_remove:
        # No drift found -> the proposed policy is the original.
        return {
            "proposed_policy": original_policy,
            "removed_actions": [],
            "rationale": "No unused permissions detected for this role.",
            "warnings": [],
        }

    prompt = _build_prompt(role_arn=role_arn, original=original_policy, to_remove=actions_to_remove)
    resp = invoke_structured(
        prompt=prompt,
        response_schema=_RESPONSE_SCHEMA,
        schema_name="iam_role_policy_scope_down",
        system_prompt=_SYSTEM_PROMPT,
        temperature=0.1,
        max_output_tokens=3072,
    )
    log.info(
        "policy_proposal_generated",
        extra={
            "roleArn": role_arn,
            "removedActionCount": len(resp.output.get("removed_actions") or []),
            "modelId": resp.model_id,
        },
    )
    return resp.output


def _build_prompt(*, role_arn: str, original: dict[str, Any], to_remove: list[str]) -> str:
    return f"""Role: {role_arn}

Original attached policy:
{json.dumps(original, indent=2)}

Actions to REMOVE (previously granted but unused or flagged for removal by Part 1):
{json.dumps(sorted(to_remove), indent=2)}

Produce the minimal replacement policy. Preserve every other action exactly. Drop any statement whose Action list would become empty after removal.
"""
