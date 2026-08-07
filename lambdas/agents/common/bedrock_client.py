"""Chat model wrapper for LangGraph agents.

Uses ``langchain_aws.ChatBedrockConverse`` so the agent talks to Kimi K2.5
through the Bedrock Converse API — same model the deterministic pipeline uses.
Auth is IAM only; the Lambda's execution role must have
``bedrock:InvokeModel`` + ``bedrock:Converse`` on the Kimi K2.5 foundation-model
ARN.

Import is lazy because ``langchain_aws`` lives in the agent Lambda Layer, not
in the base deployment package. Non-agent Lambdas MUST NOT import this file.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

DEFAULT_MODEL_ID = "moonshotai.kimi-k2.5"
DEFAULT_REGION = "us-east-1"


@lru_cache(maxsize=1)
def get_chat_model() -> Any:
    from langchain_aws import ChatBedrockConverse

    model_id = os.environ.get("LLM_MODEL_ID", DEFAULT_MODEL_ID)
    region = os.environ.get("AWS_REGION") or os.environ.get("BEDROCK_REGION", DEFAULT_REGION)
    return ChatBedrockConverse(
        model_id=model_id,
        region_name=region,
        temperature=0.1,
        max_tokens=2048,
    )
