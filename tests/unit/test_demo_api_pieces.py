"""Unit tests for the demo API's Step Functions emulator and abuse guard."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from demo_api import guard  # noqa: E402
from demo_api.sfn_emulator import Execution  # noqa: E402

REFACTOR = json.loads((ROOT / "infrastructure" / "step_functions" / "scp_refactor.json").read_text(encoding="utf-8"))
RES = "${RefactorerFunctionArn}"


def _run(fn, scp_id="p-test"):
    ex = Execution(REFACTOR, {"scpId": scp_id, "useAgent": False}, {RES: fn}, name="t-1")
    ex.run()
    return ex


def _fn(ct=False, over=True, equivalent=True):
    def fn(p):
        t = p["task"]
        if t == "load_scp":
            return {"scpId": p["scpId"], "scpName": "N", "controlTowerManaged": ct, "document": {"Statement": []}, "originalSize": 9000}
        if t == "check_control_tower":
            return {"controlTowerManaged": ct, "shortCircuit": ct}
        if t == "check_size":
            return {"overThreshold": over, "shortCircuit": not over, "originalSize": p["originalSize"]}
        if t == "propose_refactor":
            return {"proposal": {"compressed_document": {}, "new_size_bytes": 10, "statements_removed_or_merged": 1, "must_split": False, "compression_notes": "n"}}
        if t == "verify_equivalence":
            return {"equivalence": {"isEquivalent": equivalent, "totalActionsChecked": 3, "divergences": [] if equivalent else [{"action": "s3:*"}]}}
        if t == "create_change_request":
            return {"changeRequest": {"changeRequestId": "CR-1"}}
        raise AssertionError(t)
    return fn


def test_happy_path_reaches_accepted():
    ex = _run(_fn())
    assert ex.status == "SUCCEEDED" and ex.terminal == "Accepted"
    assert ex.output["status"] == "CHANGE_REQUEST_CREATED"
    assert ex.output["changeRequest"]["changeRequestId"] == "CR-1"
    assert [h["name"] for h in ex.history if h["type"] == "Task"] == [
        "LoadScp", "CheckControlTower", "CheckSize", "ProposeRefactor", "VerifyEquivalence", "CreateChangeRequest",
    ]


def test_control_tower_short_circuits():
    ex = _run(_fn(ct=True))
    assert ex.terminal == "Skipped_ControlTower"
    assert "CheckSize" not in [h["name"] for h in ex.history]


def test_under_threshold_short_circuits():
    assert _run(_fn(over=False)).terminal == "Skipped_UnderThreshold"


def test_not_equivalent_is_rejected():
    ex = _run(_fn(equivalent=False))
    assert ex.terminal == "Rejected_NotEquivalent"
    assert ex.output["divergences"][0]["action"] == "s3:*"


def test_task_exception_marks_execution_failed():
    def boom(p):
        raise RuntimeError("llm down")

    ex = _run(boom)
    assert ex.status == "FAILED" and "llm down" in ex.error
    assert ex.history[0]["status"] == "FAILED"


def test_guard_per_ip_limit_and_byo_bypass(monkeypatch):
    monkeypatch.setattr(guard, "UPSTASH_URL", "")
    monkeypatch.setattr(guard, "PER_IP_DAILY", 3)
    monkeypatch.setattr(guard, "GLOBAL_DAILY", 100)
    guard._mem.clear()
    assert guard.check("1.1.1.1", 2, byo=False).allowed
    d = guard.check("1.1.1.1", 2, byo=False)
    assert not d.allowed and "limit" in d.message
    assert guard.check("1.1.1.1", 2, byo=True).allowed  # own key: unlimited
    assert guard.check("2.2.2.2", 2, byo=False).allowed  # other visitor unaffected


def test_guard_global_cap(monkeypatch):
    monkeypatch.setattr(guard, "UPSTASH_URL", "")
    monkeypatch.setattr(guard, "PER_IP_DAILY", 100)
    monkeypatch.setattr(guard, "GLOBAL_DAILY", 3)
    guard._mem.clear()
    assert guard.check("a", 2, byo=False).allowed
    assert not guard.check("b", 2, byo=False).allowed


def test_byo_base_url_allowlist():
    assert guard.validate_byo("sk-x", None)[1].startswith("https://api.openai.com")
    assert guard.validate_byo(None, "https://evil.example") is None
    with pytest.raises(ValueError):
        guard.validate_byo("sk-x", "https://169.254.169.254/v1")
    with pytest.raises(ValueError):
        guard.validate_byo("sk-x", "http://api.openai.com/v1")
