"""Deterministic guard tests for the SCP Governance Agent."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _state(**overrides):
    state = {
        "scp_id": "p-test",
        "scp_name": "TestScp",
        "original_document": {"Statement": []},
        "proposed_document": {"Statement": []},
        "control_tower_managed": False,
        "spot_check_ok": True,
        "target_count": 3,
        "reasoning_logger": MagicMock(),
    }
    state.update(overrides)
    return state


def _apply_model():
    msg = MagicMock()
    msg.content = '{"decision": "APPLY", "rationale": "ok"}'
    model = MagicMock()
    model.invoke.return_value = msg
    return model


def test_hard_guard_allows_clean_state():
    from agents.scp_governance.graph import _hard_guard_reason

    assert _hard_guard_reason(_state()) is None


def test_deliberate_rejects_failed_spot_check_even_if_llm_applies():
    from agents.scp_governance.graph import deliberate

    with patch("agents.scp_governance.graph.get_chat_model", return_value=_apply_model()) as gm:
        out = deliberate(_state(spot_check_ok=False))
    assert out["outcome"] == "rejected"
    gm.assert_not_called()


def test_deliberate_rejects_large_blast_radius():
    from agents.scp_governance.graph import deliberate

    with patch("agents.scp_governance.graph.get_chat_model", return_value=_apply_model()):
        out = deliberate(_state(target_count=51))
    assert out["outcome"] == "rejected"


def test_deliberate_applies_when_guards_pass_and_llm_agrees():
    from agents.scp_governance.graph import deliberate

    with patch("agents.scp_governance.graph.get_chat_model", return_value=_apply_model()):
        out = deliberate(_state())
    assert not out.get("outcome")


def test_blast_radius_fails_closed_when_listing_fails():
    from agents.scp_governance.graph import assess_blast_radius

    with patch("agents.scp_governance.graph.list_targets_for_scp", side_effect=RuntimeError("denied")):
        out = assess_blast_radius(_state(target_count=None))
    assert out["outcome"] == "rejected"
    assert "blast radius unknown" in out["rollback_reason"]


def test_spot_check_with_no_sampled_actions_does_not_pass():
    from agents.scp_governance.graph import spot_check_equivalence

    out = spot_check_equivalence(_state(original_document={"Statement": [{"Effect": "Allow", "Action": "*"}]}))
    assert out["spot_check_ok"] is False
