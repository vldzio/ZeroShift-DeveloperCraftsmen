"""Bedrock Kimi K2.5 prompt for proposing a compressed SCP.

Given an SCP document that approaches the 10,240-character limit, produces a
compressed proposal that preserves semantics. If the document cannot fit even
after compression, splits it into 2-3 SCPs (max 10 per node).
"""
from __future__ import annotations

import json
from typing import Any

from shared.llm_client import invoke_structured
from shared.logging_config import get_logger

log = get_logger(__name__)

SCP_LIMIT_BYTES = 10240

_SYSTEM_PROMPT = """You are an AWS IAM expert refactoring Service Control Policies.
You compress SCP documents to fit under AWS's 10,240-character limit while
preserving exact semantic behavior. You never introduce new actions, resources,
or condition keys the original did not have. You never broaden wildcards beyond
their original scope (e.g. do not rewrite s3:GetObject as s3:*). You never drop
conditions when merging statements. If the compressed document still exceeds
the limit, you split it into 2-3 SCPs whose union preserves the original
behavior; each split SCP must be independently valid AWS policy JSON."""


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "compressed_document": {
            "type": "object",
            "description": "The compressed SCP document. Must be valid AWS policy JSON (Version + Statement)."
        },
        "statements_removed_or_merged": {
            "type": "integer",
            "description": "How many statements from the original were merged or eliminated."
        },
        "new_size_bytes": {
            "type": "integer",
            "description": "Size of the compressed document when serialized as compact JSON."
        },
        "must_split": {
            "type": "boolean",
            "description": "True if the compressed document still exceeds 10,240 bytes and split_documents is populated."
        },
        "split_documents": {
            "type": "array",
            "description": "When must_split is true, this is the list of 2-3 split SCP documents whose union preserves original semantics.",
            "items": {"type": "object"}
        },
        "compression_notes": {
            "type": "string",
            "description": "Brief human-readable summary of which statements were merged and why."
        }
    },
    "required": [
        "compressed_document",
        "statements_removed_or_merged",
        "new_size_bytes",
        "must_split",
        "compression_notes"
    ]
}


def propose(*, scp_name: str, scp_id: str, document: dict[str, Any]) -> dict[str, Any]:
    """Ask Kimi K2.5 to compress the SCP. Returns a structured proposal.

    The caller must verify equivalence via IAM Policy Simulator before trusting
    this output — the LLM can make mistakes that only a deterministic simulator
    catches (see EquivalenceChecker).
    """
    original_bytes = len(json.dumps(document))
    prompt = _build_prompt(scp_name=scp_name, scp_id=scp_id, document=document, original_bytes=original_bytes)
    resp = invoke_structured(
        prompt=prompt,
        response_schema=_RESPONSE_SCHEMA,
        schema_name="scp_refactor_proposal",
        system_prompt=_SYSTEM_PROMPT,
        temperature=0.1,
        max_output_tokens=4096,
    )
    log.info(
        "refactor_proposal_generated",
        extra={
            "scpId": scp_id,
            "originalBytes": original_bytes,
            "newBytes": resp.output.get("new_size_bytes"),
            "mustSplit": resp.output.get("must_split"),
            "statementsRemovedOrMerged": resp.output.get("statements_removed_or_merged"),
            "modelId": resp.model_id,
        },
    )
    return resp.output


def _build_prompt(*, scp_name: str, scp_id: str, document: dict[str, Any], original_bytes: int) -> str:
    return f"""The following AWS Service Control Policy is at {original_bytes} bytes, which exceeds the safe threshold (8,192 bytes = 80% of the 10,240 hard limit). Compress it by merging redundant statements while preserving exact semantics.

SCP name: {scp_name}
SCP id: {scp_id}
Current size: {original_bytes} bytes
AWS hard limit: {SCP_LIMIT_BYTES} bytes

Original document:
{json.dumps(document, indent=2)}

Rules you MUST follow:
1. Preserve every action the original covers. Do not drop actions.
2. Do not broaden wildcards. If the original has s3:GetObject, do not compress to s3:*.
3. Do not merge statements that have different Conditions. Only merge statements whose Conditions are identical.
4. When merging, prefer expanding the Action array over collapsing Resource. e.g. combine multiple {{Effect: Deny, Action: [X], Resource: [R], Condition: C}} into one {{Effect: Deny, Action: [X1, X2, ...], Resource: [R], Condition: C}}.
5. Every merged statement must produce IDENTICAL allow/deny decisions to the original for every action/resource/context combination.
6. If the compressed document still exceeds {SCP_LIMIT_BYTES} bytes, set must_split=true and provide split_documents (2-3 policies whose union equals the original).

Return the compressed document via the structured output.
"""
