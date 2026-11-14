"""OpenAI-compatible structured-output helper (FreeLLMAPI gateway, OpenAI, Groq, ...).

Free-tier models differ in how well they support ``response_format`` and forced
tool calls, so structured output is enforced the portable way: the JSON Schema
goes into the prompt, the reply is parsed leniently, validated with
``jsonschema`` and retried once with the validation error fed back.

Every call is traced to LangSmith when ``LANGSMITH_TRACING=true`` (the project
comes from ``LANGSMITH_PROJECT``). langsmith/openai are imported lazily so the
Bedrock-only Lambda package does not need them.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from . import llm_context
from .logging_config import get_logger

log = get_logger(__name__)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _client():
    from openai import OpenAI

    base_url = llm_context.base_url_override.get() or os.environ["LLM_BASE_URL"]
    api_key = llm_context.api_key_override.get() or os.environ.get("LLM_API_KEY") or "not-needed"
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=float(os.environ.get("LLM_TIMEOUT_S", "90")), max_retries=1)
    if os.environ.get("LANGSMITH_TRACING", "").lower() == "true" and os.environ.get("LANGSMITH_API_KEY"):
        try:
            from langsmith.wrappers import wrap_openai

            client = wrap_openai(client)
        except Exception as exc:  # tracing must never break the request
            log.warning("langsmith_wrap_failed", extra={"error": str(exc)})
    return client


def _parse_json(text: str) -> dict[str, Any]:
    text = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _validate(output: dict[str, Any], schema: dict[str, Any]) -> None:
    try:
        import jsonschema
    except ImportError:  # validation is best-effort without the library
        return
    jsonschema.validate(output, schema)


def invoke_json(
    *,
    prompt: str,
    response_schema: dict[str, Any],
    schema_name: str,
    system_prompt: str | None,
    temperature: float,
    max_output_tokens: int,
    model_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    client = _client()
    system = (system_prompt or "You are a precise cloud-security assistant.") + (
        "\n\nRespond with ONLY a single JSON object (no prose, no markdown fences) "
        f"that conforms to this JSON Schema named '{schema_name}':\n{json.dumps(response_schema)}"
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    extra_metadata = {"schema": schema_name, **(llm_context.trace_metadata.get() or {})}
    last_err: Exception | None = None
    raw: dict[str, Any] = {}
    for attempt in range(2):
        kwargs: dict[str, Any] = dict(
            model=model_id, messages=messages, temperature=temperature, max_tokens=max_output_tokens
        )
        try:
            resp = client.chat.completions.create(
                response_format={"type": "json_object"}, extra_body={"metadata": extra_metadata}, **kwargs
            )
        except Exception:
            # Some free models reject response_format; retry plainly.
            resp = client.chat.completions.create(**kwargs)
        raw = resp.model_dump() if hasattr(resp, "model_dump") else {"choices": []}
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        try:
            output = _parse_json(text)
            _validate(output, response_schema)
            return output, raw
        except Exception as exc:
            last_err = exc
            log.warning("llm_structured_retry", extra={"attempt": attempt, "error": str(exc)[:300]})
            messages = messages + [
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content": f"That was not valid for the schema ({str(exc)[:300]}). Reply again with ONLY the corrected JSON object."},
            ]
    raise ValueError(f"LLM did not return schema-valid JSON for '{schema_name}': {last_err}")
