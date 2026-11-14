"""Chat model wrapper for LangGraph agents.

Uses ``langchain_aws.ChatBedrockConverse`` so the agent talks to Kimi K2.5
through the Bedrock Converse API — same model the deterministic pipeline uses.
Auth is IAM only; the Lambda's execution role must have
``bedrock:InvokeModel`` + ``bedrock:Converse`` on the Kimi K2.5 foundation-model
ARN.

If ``LLM_BASE_URL`` is set (or ``LLM_PROVIDER=openai-compatible``) the agents
use a ``langchain_openai.ChatOpenAI`` pointed at that OpenAI-compatible
endpoint instead (the demo deployment uses a FreeLLMAPI gateway). LangChain /
LangGraph runs are traced to LangSmith automatically when
``LANGSMITH_TRACING=true``.

Import is lazy because ``langchain_aws`` lives in the agent Lambda Layer, not
in the base deployment package. Non-agent Lambdas MUST NOT import this file.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

DEFAULT_MODEL_ID = "moonshotai.kimi-k2.5"
DEFAULT_REGION = "us-east-1"


def get_chat_model() -> Any:
    if os.environ.get("LLM_PROVIDER", "").lower() == "openai-compatible" or (
        not os.environ.get("LLM_PROVIDER") and os.environ.get("LLM_BASE_URL")
    ):
        return _openai_compatible_model()  # not cached: honours per-request BYO keys
    return _bedrock_model()


@lru_cache(maxsize=1)
def _bedrock_model() -> Any:
    from langchain_aws import ChatBedrockConverse

    model_id = os.environ.get("LLM_MODEL_ID", DEFAULT_MODEL_ID)
    region = os.environ.get("AWS_REGION") or os.environ.get("BEDROCK_REGION", DEFAULT_REGION)
    return ChatBedrockConverse(
        model_id=model_id,
        region_name=region,
        temperature=0.1,
        max_tokens=2048,
    )


def _openai_compatible_model(api_key: str | None = None, base_url: str | None = None) -> Any:
    from langchain_openai import ChatOpenAI

    from shared import llm_context

    return ChatOpenAI(
        model=os.environ.get("LLM_MODEL_ID", "auto"),
        base_url=base_url or llm_context.base_url_override.get() or os.environ["LLM_BASE_URL"],
        api_key=api_key or llm_context.api_key_override.get() or os.environ.get("LLM_API_KEY") or "not-needed",
        temperature=0.1,
        max_tokens=2048,
        timeout=90,
        max_retries=1,
    )
