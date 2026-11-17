"""OpenAI-compatible structured-output helper (FreeLLMAPI gateway, OpenAI, Groq, ...).

Free-tier models differ in how well they support ``response_format`` and forced
tool calls, so structured output is enforced the portable way: the JSON Schema
goes into the prompt, the reply is parsed leniently, validated with
``jsonschema`` and retried once with the validation error fed back.

Latency is bounded on purpose. The free gateway's ``auto`` route can fail over
across slow, rate-limited providers, and an unbounded SDK timeout multiplied by
SDK retries and the schema retry once left a refactor "running" for minutes.
So the default is a pinned chain of fast models (``auto`` last), a short
per-try timeout, no SDK retries, a rest period for a model that just failed,
and one overall wall-clock budget per call (``LLM_BUDGET_S``).

Every call is traced to LangSmith when ``LANGSMITH_TRACING=true`` (the project
comes from ``LANGSMITH_PROJECT``). langsmith/openai are imported lazily so the
Bedrock-only Lambda package does not need them.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from . import llm_context
from .logging_config import get_logger

log = get_logger(__name__)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

# Free-demo chain, tried in order; "name@effort" sets reasoning_effort for a reasoning model.
# ``auto`` is last on purpose: it often routes to slow or throttled free models.
DEFAULT_FREE_MODELS = "gemini-3.1-flash-lite,qwen3.8-27b,gpt-oss-120b@low,gemini-3.5-flash-lite,gpt-oss-20b@low,auto"
COOLDOWN_SECONDS = 60.0
_cooldown_until: dict[str, float] = {}  # model spec -> monotonic time before which it is skipped
MIN_TRY_SECONDS = 3.0


class LlmError(RuntimeError):
    """The LLM could not answer (every model failed or the time budget ran out)."""


class LlmBudgetExceeded(LlmError):
    """The overall per-call time budget was used up."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def model_chain(model_id: str) -> list[str]:
    """Models to try, in order. A visitor's own key or an explicit LLM_MODEL_ID pins a single model."""
    if llm_context.api_key_override.get() or llm_context.base_url_override.get():
        return [model_id]
    raw = os.environ.get("LLM_MODELS")
    if not raw and os.environ.get("LLM_MODEL_ID"):
        return [model_id]
    chain = [m.strip() for m in (raw or DEFAULT_FREE_MODELS).split(",") if m.strip()]
    return chain or [model_id]


def _client(chain_len: int = 1):
    from openai import OpenAI

    base_url = llm_context.base_url_override.get() or os.environ["LLM_BASE_URL"]
    api_key = llm_context.api_key_override.get() or os.environ.get("LLM_API_KEY") or "not-needed"
    # With a chain, fail fast and move on to the next model instead of retrying a stuck one.
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=_env_float("LLM_TIMEOUT_S", 90.0),
        max_retries=0 if chain_len > 1 else 1,
    )
    if os.environ.get("LANGSMITH_TRACING", "").lower() == "true" and os.environ.get("LANGSMITH_API_KEY"):
        try:
            from langsmith.wrappers import wrap_openai

            client = wrap_openai(client)
        except Exception as exc:  # tracing must never break the request
            log.warning("langsmith_wrap_failed", extra={"error": str(exc)})
    return client


def _complete(
    client: Any,
    chain: list[str],
    deadline: float,
    messages: list[dict[str, str]],
    temperature: float,
    max_output_tokens: int,
    extra_metadata: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """One completion from the first model in ``chain`` that answers before ``deadline``."""
    from openai import APIStatusError

    now = time.monotonic()
    ready = [m for m in chain if _cooldown_until.get(m, 0) <= now] or chain
    try_cap = _env_float("LLM_TRY_TIMEOUT_S", 25.0) if len(chain) > 1 else _env_float("LLM_TIMEOUT_S", 90.0)
    last_err: Exception | None = None
    for spec in ready:
        remaining = deadline - time.monotonic()
        if remaining < MIN_TRY_SECONDS:
            raise LlmBudgetExceeded(
                f"The LLM did not answer within the {_env_float('LLM_BUDGET_S', 90.0):.0f}s time budget"
                + (f" (last error: {type(last_err).__name__})" if last_err else "")
            )
        model, _, effort = spec.partition("@")
        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_output_tokens,
            timeout=min(try_cap, remaining),
        )
        if effort:
            kwargs["reasoning_effort"] = effort
        try:
            try:
                resp = client.chat.completions.create(
                    response_format={"type": "json_object"}, extra_body={"metadata": extra_metadata}, **kwargs
                )
            except APIStatusError as exc:
                if exc.status_code not in (400, 422):
                    raise
                # The model rejected response_format: ask plainly (still bounded by the same deadline).
                kwargs["timeout"] = max(MIN_TRY_SECONDS, min(try_cap, deadline - time.monotonic()))
                resp = client.chat.completions.create(**kwargs)
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            if not text.strip():
                raise ValueError("empty response")
            raw = resp.model_dump() if hasattr(resp, "model_dump") else {"choices": []}
            return text, raw
        except Exception as exc:  # noqa: BLE001 - fall through to the next model
            last_err = exc
            if len(chain) > 1:
                _cooldown_until[spec] = time.monotonic() + COOLDOWN_SECONDS
            log.warning("llm_model_failed", extra={"model": spec, "error": type(exc).__name__})
    raise LlmError(f"No model in the free pool answered (last error: {type(last_err).__name__ if last_err else 'none'})")


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
    chain = model_chain(model_id)
    client = _client(len(chain))
    deadline = time.monotonic() + _env_float("LLM_BUDGET_S", 90.0)
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
    for attempt in range(2):
        text, raw = _complete(client, chain, deadline, messages, temperature, max_output_tokens, extra_metadata)
        try:
            output = _parse_json(text)
            _validate(output, response_schema)
            return output, raw
        except Exception as exc:
            last_err = exc
            log.warning("llm_structured_retry", extra={"attempt": attempt, "error": str(exc)[:300]})
            messages = messages + [
                {"role": "assistant", "content": text[:4000]},
                {
                    "role": "user",
                    "content": f"That was not valid for the schema ({str(exc)[:300]}). Reply again with ONLY the corrected JSON object.",
                },
            ]
    raise ValueError(f"LLM did not return schema-valid JSON for '{schema_name}': {last_err}")
