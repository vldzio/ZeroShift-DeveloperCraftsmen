"""Provider-agnostic LLM client with structured JSON output enforcement.

Supported providers (selected via the ``LLM_PROVIDER`` environment variable):

- ``bedrock`` (default): any model hosted on Amazon Bedrock, invoked through
  the provider-neutral Converse API (``bedrock-runtime:Converse``).
  Authentication via the Lambda IAM role. No API key required.
  Default model: Moonshot AI Kimi K2.5 (``moonshotai.kimi-k2.5``).
- ``moonshot-direct``: Moonshot AI's own API. Reads credentials from AWS
  Secrets Manager secret ``zeroshift/llm-credentials``. Not implemented at v1.
- ``openai-direct``: OpenAI's Responses API at ``https://api.openai.com``.
  Reads credentials from the same secret. Not implemented at v1.
- ``azure-openai``: Azure OpenAI Service. Reads endpoint + deployment + key
  from the same secret. Not implemented at v1.

All providers are called through the same interface: ``invoke_structured(
prompt, response_schema)`` returns a dict matching the supplied JSON schema.
Structured output on the Bedrock path is enforced via Converse's ``toolConfig``
with a forced tool call whose ``inputSchema`` is the caller's response schema.

Swapping to a different Bedrock model (Claude, Nova, Llama, another Kimi tier)
is a one-line change to the ``LLM_MODEL_ID`` environment variable — the
Converse contract is identical across providers.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import boto3

from .logging_config import get_logger

log = get_logger(__name__)

PROVIDER_ENV = "LLM_PROVIDER"
MODEL_ID_ENV = "LLM_MODEL_ID"
LLM_SECRET_ENV = "LLM_CREDENTIALS_SECRET_ID"

DEFAULT_PROVIDER = "bedrock"
DEFAULT_MODEL_ID_PLACEHOLDER = "moonshotai.kimi-k2.5"


@dataclass
class LlmResponse:
    output: dict[str, Any]
    raw: dict[str, Any]
    model_id: str
    provider: str


def _provider() -> str:
    return os.environ.get(PROVIDER_ENV, DEFAULT_PROVIDER).lower()


def _model_id() -> str:
    return os.environ.get(MODEL_ID_ENV, DEFAULT_MODEL_ID_PLACEHOLDER)


@lru_cache(maxsize=1)
def _load_credentials() -> dict[str, str]:
    """Load direct-provider credentials from Secrets Manager.

    Only called when the provider is not ``bedrock``. Expected scaffold keys:
    ``moonshotApiKey``, ``openaiApiKey``, ``openaiOrgId``, ``azureEndpoint``,
    ``azureDeploymentName``, ``azureApiVersion``.
    """
    secret_id = os.environ.get(LLM_SECRET_ENV)
    if not secret_id:
        raise RuntimeError(
            f"{LLM_SECRET_ENV} must be set when LLM_PROVIDER is not bedrock"
        )
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=secret_id)
    return json.loads(resp["SecretString"] or "{}")


def invoke_structured(
    *,
    prompt: str,
    response_schema: dict[str, Any],
    schema_name: str = "response",
    system_prompt: str | None = None,
    temperature: float = 0.2,
    max_output_tokens: int = 1024,
) -> LlmResponse:
    """Send a prompt to the configured LLM and return a validated JSON dict.

    ``response_schema`` is a JSON Schema object; the model is constrained to
    emit output matching it. On the Bedrock path this is enforced via a
    forced tool call in Converse's ``toolConfig``.
    """
    provider = _provider()
    model_id = _model_id()

    log.info(
        "llm_invoke",
        extra={"provider": provider, "modelId": model_id, "promptLen": len(prompt)},
    )

    if provider == "bedrock":
        raw = _invoke_bedrock_converse(
            prompt=prompt,
            response_schema=response_schema,
            schema_name=schema_name,
            system_prompt=system_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            model_id=model_id,
        )
        output = _extract_converse_output(raw, tool_name=schema_name)
    elif provider == "moonshot-direct":
        raw = _invoke_moonshot_direct(model_id=model_id)
        output = _extract_direct_json_output(raw)
    elif provider == "openai-direct":
        raw = _invoke_openai_direct(model_id=model_id)
        output = _extract_direct_json_output(raw)
    elif provider == "azure-openai":
        raw = _invoke_azure_openai(model_id=model_id)
        output = _extract_direct_json_output(raw)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}")

    return LlmResponse(output=output, raw=raw, model_id=model_id, provider=provider)


# ---- Bedrock Converse API (provider-neutral; default path) ----

def _build_converse_request(
    *,
    prompt: str,
    response_schema: dict[str, Any],
    schema_name: str,
    system_prompt: str | None,
    temperature: float,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Assemble a Converse API request that forces a tool call whose input
    schema is the caller's response schema. The model's tool-use input is
    the validated structured output."""
    request: dict[str, Any] = {
        "messages": [
            {"role": "user", "content": [{"text": prompt}]},
        ],
        "inferenceConfig": {
            "temperature": temperature,
            "maxTokens": max_output_tokens,
        },
        "toolConfig": {
            "tools": [
                {
                    "toolSpec": {
                        "name": schema_name,
                        "description": (
                            "Return the structured response for the user's request. "
                            "The response is validated against the input schema."
                        ),
                        "inputSchema": {"json": response_schema},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": schema_name}},
        },
    }
    if system_prompt:
        request["system"] = [{"text": system_prompt}]
    return request


def _invoke_bedrock_converse(
    *,
    prompt: str,
    response_schema: dict[str, Any],
    schema_name: str,
    system_prompt: str | None,
    temperature: float,
    max_output_tokens: int,
    model_id: str,
) -> dict[str, Any]:
    request = _build_converse_request(
        prompt=prompt,
        response_schema=response_schema,
        schema_name=schema_name,
        system_prompt=system_prompt,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    client = boto3.client("bedrock-runtime")
    resp = client.converse(modelId=model_id, **request)
    return resp


def _extract_converse_output(raw: dict[str, Any], *, tool_name: str) -> dict[str, Any]:
    """Pull the tool-use input from a Converse response.

    Converse returns ``output.message.content`` as a list of content blocks;
    forced-tool responses include a ``toolUse`` block whose ``input`` is the
    structured payload validated against the tool's ``inputSchema``.
    """
    message = (raw.get("output") or {}).get("message") or {}
    for block in message.get("content", []) or []:
        tool_use = block.get("toolUse")
        if tool_use and (tool_use.get("name") == tool_name or tool_name is None):
            return tool_use.get("input") or {}
    # Fallback: some models may emit the JSON as a text block if the tool call
    # was skipped. Try to parse the first non-empty text block.
    for block in message.get("content", []) or []:
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return json.loads(text)
    raise ValueError(f"Could not extract structured output from Converse response: {raw!r}")


# ---- Direct-provider fallbacks (stubs, not implemented at v1) ----

def _invoke_moonshot_direct(*, model_id: str) -> dict[str, Any]:
    creds = _load_credentials()
    if not creds.get("moonshotApiKey"):
        raise RuntimeError(
            "moonshotApiKey is empty in the zeroshift/llm-credentials secret"
        )
    raise NotImplementedError(
        f"moonshot-direct provider is not implemented at v1 (requested model_id={model_id}); use bedrock."
    )


def _invoke_openai_direct(*, model_id: str) -> dict[str, Any]:
    creds = _load_credentials()
    if not creds.get("openaiApiKey"):
        raise RuntimeError(
            "openaiApiKey is empty in the zeroshift/llm-credentials secret"
        )
    raise NotImplementedError(
        f"openai-direct provider is not implemented at v1 (requested model_id={model_id}); use bedrock."
    )


def _invoke_azure_openai(*, model_id: str) -> dict[str, Any]:
    creds = _load_credentials()
    if not creds.get("azureEndpoint") or not creds.get("azureDeploymentName"):
        raise RuntimeError(
            "azureEndpoint and azureDeploymentName must be set in the "
            "zeroshift/llm-credentials secret to use the azure-openai provider"
        )
    raise NotImplementedError(
        f"azure-openai provider is not implemented at v1 (requested model_id={model_id}); use bedrock."
    )


def _extract_direct_json_output(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract JSON from a direct-provider (Chat/Responses-shaped) payload."""
    for choice in raw.get("choices", []) or []:
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return json.loads(content)
    for item in raw.get("output", []) or []:
        for block in item.get("content", []) or []:
            if block.get("type") in ("output_text", "text"):
                text = block.get("text") or ""
                if text:
                    return json.loads(text)
    raise ValueError(f"Could not extract structured output from direct-provider response: {raw!r}")
