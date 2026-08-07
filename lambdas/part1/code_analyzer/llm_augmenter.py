"""Kimi K2.5 pass to catch SDK calls the static extractor misses.

Common miss patterns:
- Dynamic dispatch: ``getattr(client, method_name)()``
- Client returned from a factory function
- Third-party wrappers around boto3 (e.g. aioboto3, boto3.session.Session().client)
- SDK-call methods that don't appear in the code but are called via strings

The augmenter receives the code plus the static findings and returns a list of
IAM actions the model believes are also invoked. The intent-writer step
merges these with the static findings, so the LLM's opinion is additive
rather than authoritative.
"""
from __future__ import annotations

from typing import Any

from shared.llm_client import invoke_structured
from shared.logging_config import get_logger

log = get_logger(__name__)


_SYSTEM_PROMPT = """You review Python source for AWS SDK usage. Given the
source and a list of AWS actions a static AST extractor already found, you
identify any AWS SDK calls the extractor missed — typically dynamic dispatches
(getattr, factories, wrapped clients). You output only actions the code
actually calls. You do NOT list actions the extractor already found."""


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "additional_actions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IAM action strings (e.g. 's3:PutObject') the static extractor missed. Empty array if none.",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "Model's confidence that the additional_actions are actually invoked.",
        },
        "notes": {
            "type": "string",
            "description": "One-sentence rationale.",
        },
    },
    "required": ["additional_actions", "confidence", "notes"],
}


def augment(*, source: str, static_actions: list[str]) -> dict[str, Any]:
    """Return ``{additional_actions, confidence, notes}``.

    On any error (LLM unavailable, invalid response), returns an empty
    additional_actions list rather than failing the whole analysis. The
    static extractor's output is always the safety-relevant floor.
    """
    prompt = f"""Static AST extractor already found these AWS SDK actions:
{static_actions}

Full source:
```python
{source}
```

Identify any actions the extractor missed (dynamic dispatch, factories,
wrapped clients). Do not repeat actions already in the extractor's list.
"""
    try:
        resp = invoke_structured(
            prompt=prompt,
            response_schema=_RESPONSE_SCHEMA,
            schema_name="sdk_call_augmentation",
            system_prompt=_SYSTEM_PROMPT,
            temperature=0.0,
            max_output_tokens=800,
        )
        return resp.output
    except Exception as exc:
        log.warning("llm_augmenter_failed", extra={"error": str(exc)})
        return {"additional_actions": [], "confidence": "low", "notes": f"augmentation skipped: {exc}"}
