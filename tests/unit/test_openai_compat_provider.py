"""Tests for the openai-compatible LLM provider (FreeLLMAPI gateway path).

A tiny local HTTP server plays the OpenAI ``/v1/chat/completions`` endpoint, so
the real ``openai`` SDK code path runs end to end without any provider key.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from shared import llm_client, llm_context

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


class _Stub(BaseHTTPRequestHandler):
    replies: list[str] = []
    seen_auth: list[str] = []
    seen_bodies: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen_auth.append(self.headers.get("authorization", ""))
        type(self).seen_bodies.append(body)
        content = type(self).replies.pop(0) if type(self).replies else "{}"
        payload = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "auto"),
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture()
def gateway(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Stub.replies, _Stub.seen_auth, _Stub.seen_bodies = [], [], []
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("LLM_API_KEY", "gateway-key")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    yield _Stub
    server.shutdown()


def test_provider_autoselected_from_base_url(gateway):
    assert llm_client._provider() == "openai-compatible"
    assert llm_client._model_id() == "auto"


def test_structured_output_parsed_from_fenced_json(gateway):
    gateway.replies = ['```json\n{"summary": "ok"}\n```']
    resp = llm_client.invoke_structured(prompt="hi", response_schema=SCHEMA, schema_name="t")
    assert resp.output == {"summary": "ok"}
    assert resp.provider == "openai-compatible"
    assert gateway.seen_auth[0] == "Bearer gateway-key"


def test_invalid_json_is_retried_once_with_feedback(gateway):
    gateway.replies = ["sorry, here you go", '{"summary": "second try"}']
    resp = llm_client.invoke_structured(prompt="hi", response_schema=SCHEMA, schema_name="t")
    assert resp.output["summary"] == "second try"
    assert len(gateway.seen_bodies) == 2
    assert "not valid" in gateway.seen_bodies[1]["messages"][-1]["content"]


def test_schema_violation_raises_after_retry(gateway):
    gateway.replies = ['{"nope": 1}', '{"nope": 2}']
    with pytest.raises(ValueError):
        llm_client.invoke_structured(prompt="hi", response_schema=SCHEMA, schema_name="t")


def test_byo_key_override_is_used_for_the_request(gateway):
    gateway.replies = ['{"summary": "byo"}']
    t1 = llm_context.api_key_override.set("visitor-key")
    try:
        llm_client.invoke_structured(prompt="hi", response_schema=SCHEMA, schema_name="t")
    finally:
        llm_context.api_key_override.reset(t1)
    assert gateway.seen_auth[0] == "Bearer visitor-key"
