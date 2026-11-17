"""Time-bounding tests: a stuck free-tier model must not stall a refactor.

Covers the pinned model chain + per-try timeout + overall budget in
``shared.openai_compat``, the per-step / per-execution caps in the Step Functions
emulator, and the API-reported model label.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from demo_api.sfn_emulator import Execution
from shared import llm_client, openai_compat

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


class _Gateway(BaseHTTPRequestHandler):
    """Behaviour per model name: 'slow' hangs, 'broken' answers 500, 'strict' rejects response_format."""

    seen: list[str] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        model = body.get("model", "")
        type(self).seen.append(model)
        if model.startswith("slow"):
            time.sleep(6)
        if model.startswith("broken"):
            return self._send(500, {"error": {"message": "boom"}})
        if model.startswith("strict") and "response_format" in body:
            return self._send(400, {"error": {"message": "response_format unsupported"}})
        content = json.dumps({"summary": f"answered by {model}"})
        self._send(200, {
            "id": "x", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    def _send(self, code: int, payload: dict) -> None:
        try:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # client gave up on a slow model
            pass

    def log_message(self, *a):
        pass


@pytest.fixture()
def gateway(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Gateway.seen = []
    openai_compat._cooldown_until.clear()
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.setenv("LLM_TRY_TIMEOUT_S", "1")
    monkeypatch.setenv("LLM_BUDGET_S", "30")
    yield _Gateway
    server.shutdown()
    openai_compat._cooldown_until.clear()


def _ask():
    return llm_client.invoke_structured(prompt="hi", response_schema=SCHEMA, schema_name="t")


def test_default_chain_is_pinned_with_auto_last(gateway):
    chain = openai_compat.model_chain("auto")
    assert chain[-1] == "auto" and len(chain) > 1 and "auto" not in chain[:-1]


def test_slow_model_times_out_and_next_model_answers(gateway, monkeypatch):
    monkeypatch.setenv("LLM_MODELS", "slow-a,good-b")
    t0 = time.monotonic()
    resp = _ask()
    assert resp.output["summary"] == "answered by good-b"
    assert time.monotonic() - t0 < 4  # one 1s try timeout, not the SDK's 90s x retries
    assert gateway.seen == ["slow-a", "good-b"]


def test_failed_model_rests_so_the_next_call_skips_it(gateway, monkeypatch):
    monkeypatch.setenv("LLM_MODELS", "broken-a,good-b")
    _ask()
    gateway.seen.clear()
    _ask()
    assert gateway.seen == ["good-b"]


def test_overall_budget_ends_a_call_when_every_model_hangs(gateway, monkeypatch):
    monkeypatch.setenv("LLM_MODELS", "slow-a,slow-b,slow-c,slow-d")
    monkeypatch.setenv("LLM_BUDGET_S", "4")
    t0 = time.monotonic()
    with pytest.raises(openai_compat.LlmBudgetExceeded) as err:
        _ask()
    assert time.monotonic() - t0 < 5
    assert "time budget" in str(err.value)
    assert isinstance(err.value, openai_compat.LlmError)


def test_model_rejecting_response_format_is_retried_plainly(gateway, monkeypatch):
    monkeypatch.setenv("LLM_MODELS", "strict-a,good-b")
    assert _ask().output["summary"] == "answered by strict-a"
    assert gateway.seen == ["strict-a", "strict-a"]


def test_explicit_model_id_or_visitor_key_pins_a_single_model(gateway, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_ID", "my-model")
    assert openai_compat.model_chain("my-model") == ["my-model"]
    monkeypatch.delenv("LLM_MODEL_ID")
    from shared import llm_context

    token = llm_context.api_key_override.set("visitor")
    try:
        assert openai_compat.model_chain("auto") == ["auto"]
    finally:
        llm_context.api_key_override.reset(token)


# ---- emulator caps ----------------------------------------------------------

_DEF = {
    "StartAt": "Quick",
    "States": {
        "Quick": {"Type": "Task", "Resource": "quick", "ResultPath": "$.q", "Next": "Stuck"},
        "Stuck": {"Type": "Task", "Resource": "stuck", "ResultPath": "$.s", "End": True},
    },
}


def test_stuck_step_ends_as_failed_with_a_clear_message():
    gate = threading.Event()
    ex = Execution(_DEF, {}, {"quick": lambda p: {"ok": 1}, "stuck": lambda p: gate.wait(10) or {}}, "t", step_timeout_s=0.3)
    t0 = time.monotonic()
    ex.run()
    gate.set()
    assert time.monotonic() - t0 < 3
    assert ex.status == "FAILED"
    assert "Stuck" in ex.output["cause"] and "did not finish" in ex.output["cause"]
    by_name = {h["name"]: h["status"] for h in ex.history}
    assert by_name == {"Quick": "SUCCEEDED", "Stuck": "FAILED"}


def test_total_execution_cap_fails_the_run():
    ex = Execution(
        _DEF, {}, {"quick": lambda p: time.sleep(0.4) or {}, "stuck": lambda p: {}}, "t",
        step_timeout_s=5, execution_timeout_s=0.2,
    )
    ex.run()
    assert ex.status == "FAILED"
    assert "time limit" in ex.output["cause"]


def test_normal_run_is_unaffected_and_task_errors_still_propagate():
    ok = Execution(_DEF, {}, {"quick": lambda p: {"a": 1}, "stuck": lambda p: {"b": 2}}, "t")
    ok.run()
    assert ok.status == "SUCCEEDED"

    def boom(p):
        raise ValueError("bad input")

    bad = Execution(_DEF, {}, {"quick": boom, "stuck": lambda p: {}}, "t")
    bad.run()
    assert bad.status == "FAILED" and "bad input" in bad.output["cause"]


# ---- API-reported model label -----------------------------------------------

def test_root_reports_the_active_model_for_the_ui(monkeypatch):
    from demo_api import app as api

    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    info = api.llm_info()
    assert info["provider"] == "openai-compatible" and "Free-tier" in info["name"] and info["models"][-1] == "auto"

    monkeypatch.delenv("LLM_BASE_URL")
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    assert api.llm_info()["name"] is None  # UI keeps its AWS/Bedrock wording
