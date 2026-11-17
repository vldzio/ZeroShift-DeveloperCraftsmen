"""Per-request LLM overrides (bring-your-own key) for the demo API.

The demo API sets these for the duration of one request so a visitor can use
their own OpenAI-compatible key instead of the shared free-tier gateway.
Lambdas in a real AWS deployment never set them.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

api_key_override: ContextVar[Optional[str]] = ContextVar("llm_api_key_override", default=None)
base_url_override: ContextVar[Optional[str]] = ContextVar("llm_base_url_override", default=None)
trace_metadata: ContextVar[Optional[dict]] = ContextVar("llm_trace_metadata", default=None)
