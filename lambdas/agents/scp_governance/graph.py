"""LangGraph state graph for the SCP Governance Agent.

Autonomous apply for Part 3 Component 2's SCP refactoring path. Replaces the
safe-mode stub with a real ``organizations:UpdatePolicy`` mutation guarded by
a Control-Tower hard-refuse node, a snapshot-for-rollback node, a spot-check
node that samples the equivalence report, and Kimi K2.5 deliberation before
the write.
"""
from __future__ import annotations

import json
import os
from typing import Any, TypedDict

from agents.common.bedrock_client import get_chat_model
from agents.common.reasoning_logger import ReasoningLogger
from agents.common.tools import (
    describe_scp,
    is_control_tower_managed,
    list_targets_for_scp,
    notify_sns,
    simulate_iam_action,
    update_scp,
)
from shared.logging_config import get_logger

log = get_logger(__name__)


class ScpAgentState(TypedDict, total=False):
    scp_id: str
    scp_name: str
    original_document: dict[str, Any]
    proposed_document: dict[str, Any]
    equivalence_report: dict[str, Any]
    control_tower_managed: bool
    target_count: int
    spot_check_results: list[dict[str, Any]]
    spot_check_ok: bool
    applied: bool
    previous_content_backup: dict[str, Any]
    rollback_reason: str
    outcome: str
    reasoning_logger: Any
    correlation_id: str


_DELIBERATE_PROMPT = """You are an autonomous SCP governance agent. You are handed:
- an SCP proposed for refactoring (compressed but semantically equivalent to the original)
- a deterministic equivalence report from the upstream pipeline
- a fresh spot-check of 5+ actions you re-simulated yourself
- the count of accounts/OUs this SCP is currently attached to

Decide: APPLY the update, or REJECT.

Rules:
- If control_tower_managed is true, REJECT with reason "Control Tower–managed SCP; ZeroShift must not touch".
- If spot_check_ok is false, REJECT with reason describing the divergence.
- If target_count > 50 (large blast radius), REJECT with reason "blast radius too large for autonomous apply; human approval required".
- Otherwise APPLY.

Respond with a JSON object matching:
{{"decision": "APPLY" | "REJECT", "rationale": "<one sentence>"}}"""


def _decide_via_llm(prompt: str, context: dict[str, Any]) -> dict[str, Any]:
    model = get_chat_model()
    full = f"{prompt}\n\nContext:\n{json.dumps(context, default=str, indent=2)}"
    resp = model.invoke(full)
    text = resp.content if hasattr(resp, "content") else str(resp)
    if isinstance(text, list):
        text = "".join(str(chunk.get("text", chunk)) if isinstance(chunk, dict) else str(chunk) for chunk in text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {"decision": "REJECT", "rationale": f"LLM output was not parseable JSON: {text[:200]}"}


# ---- Nodes ----

def assess_ct_guard(state: ScpAgentState) -> ScpAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    result = is_control_tower_managed(state.get("scp_name", ""))
    ct = bool(result.get("control_tower_managed"))
    rl.log_step(
        node="assess_ct_guard",
        summary=f"Control Tower managed: {ct}",
        tool_name="is_control_tower_managed",
        tool_result=result,
        next_node="finish_failure" if ct else "snapshot_current",
    )
    if ct:
        return {**state, "control_tower_managed": True, "outcome": "rejected", "rollback_reason": "Control Tower–managed SCP"}
    return {**state, "control_tower_managed": False}


def snapshot_current(state: ScpAgentState) -> ScpAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    snapshot = describe_scp(state["scp_id"])
    rl.log_step(
        node="snapshot_current",
        summary=f"Snapshotted SCP {state['scp_id']} for rollback",
        tool_name="describe_scp",
        tool_result={"snapshot_bytes": len(json.dumps(snapshot.get("document") or {}))},
        next_node="spot_check_equivalence",
    )
    return {**state, "previous_content_backup": snapshot.get("document") or {}}


def spot_check_equivalence(state: ScpAgentState) -> ScpAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    # Pick 5 representative actions from the original SCP's Deny statements.
    actions: list[str] = []
    for stmt in state["original_document"].get("Statement", []) or []:
        if stmt.get("Effect") != "Deny":
            continue
        raw_actions = stmt.get("Action", [])
        if isinstance(raw_actions, str):
            raw_actions = [raw_actions]
        for a in raw_actions:
            if ":" in a and "*" not in a:
                actions.append(a)
    sample = actions[:5] if len(actions) >= 5 else actions

    results = []
    ok = True
    for action in sample:
        orig = simulate_iam_action(action, state["original_document"])
        prop = simulate_iam_action(action, state["proposed_document"])
        results.append({"action": action, "original": orig["decision"], "proposed": prop["decision"]})
        if orig["decision"] != prop["decision"]:
            ok = False

    rl.log_step(
        node="spot_check_equivalence",
        summary=f"Sampled {len(sample)} actions; equivalence ok={ok}",
        tool_name="simulate_iam_action",
        tool_result={"results": results, "sample_size": len(sample), "ok": ok},
        next_node="assess_blast_radius",
    )
    return {**state, "spot_check_results": results, "spot_check_ok": ok}


def assess_blast_radius(state: ScpAgentState) -> ScpAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    try:
        targets = list_targets_for_scp(state["scp_id"])
        target_count = targets.get("target_count", 0)
    except Exception as exc:
        log.warning("list_targets_failed", extra={"error": str(exc)})
        target_count = 0
        targets = {"targets": [], "error": str(exc)}
    rl.log_step(
        node="assess_blast_radius",
        summary=f"SCP is attached to {target_count} target(s)",
        tool_name="list_targets_for_scp",
        tool_result=targets,
        next_node="deliberate",
    )
    return {**state, "target_count": target_count}


def deliberate(state: ScpAgentState) -> ScpAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    context = {
        "scp_id": state.get("scp_id"),
        "scp_name": state.get("scp_name"),
        "control_tower_managed": state.get("control_tower_managed", False),
        "spot_check_ok": state.get("spot_check_ok"),
        "spot_check_results": state.get("spot_check_results", []),
        "target_count": state.get("target_count", 0),
        "equivalence_report": state.get("equivalence_report", {}),
        "original_size_bytes": len(json.dumps(state["original_document"])),
        "proposed_size_bytes": len(json.dumps(state["proposed_document"])),
    }
    decision = _decide_via_llm(_DELIBERATE_PROMPT, context)
    rl.log_step(
        node="deliberate",
        summary=f"LLM decision: {decision.get('decision')} — {decision.get('rationale', '')}",
        llm_output=decision,
        next_node="apply_scp" if decision.get("decision") == "APPLY" else "finish_failure",
    )
    if decision.get("decision") == "APPLY":
        return {**state}
    return {**state, "outcome": "rejected", "rollback_reason": decision.get("rationale", "")}


def apply_scp(state: ScpAgentState) -> ScpAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    try:
        result = update_scp(state["scp_id"], state["proposed_document"])
        rl.log_step(
            node="apply_scp",
            summary=f"Called organizations:UpdatePolicy on {state['scp_id']}",
            tool_name="update_scp",
            tool_result=result,
            next_node="verify_scp",
        )
        return {**state, "applied": True}
    except Exception as exc:
        rl.log_step(
            node="apply_scp",
            summary=f"UpdatePolicy failed: {exc}",
            tool_name="update_scp",
            tool_result={"error": str(exc)},
            next_node="finish_failure",
        )
        return {**state, "applied": False, "outcome": "failure", "rollback_reason": f"update_scp failed: {exc}"}


def verify_scp(state: ScpAgentState) -> ScpAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    try:
        current = describe_scp(state["scp_id"])
        expected = state["proposed_document"]
        matches = json.dumps(current.get("document") or {}, sort_keys=True) == json.dumps(expected, sort_keys=True)
    except Exception as exc:
        matches = False
        current = {"error": str(exc)}
    rl.log_step(
        node="verify_scp",
        summary=f"Re-fetched SCP; matches proposed={matches}",
        tool_name="describe_scp",
        tool_result={"matches": matches, "current_bytes": len(json.dumps(current.get("document") or {}))},
        next_node="finish_success" if matches else "rollback_scp",
    )
    if matches:
        return {**state, "outcome": "success"}
    return {**state, "rollback_reason": "verification mismatch"}


def rollback_scp(state: ScpAgentState) -> ScpAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    backup = state.get("previous_content_backup") or {}
    try:
        update_scp(state["scp_id"], backup)
        summary = "Rolled back SCP to snapshot"
    except Exception as exc:
        summary = f"Rollback failed: {exc}"
    rl.log_step(
        node="rollback_scp",
        summary=summary,
        tool_name="update_scp",
        tool_result={"restored_bytes": len(json.dumps(backup))},
        next_node="finish_failure",
    )
    return {**state, "outcome": "failure"}


def finish_success(state: ScpAgentState) -> ScpAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    topic_arn = os.environ.get("APPROVAL_REQUESTS_TOPIC_ARN")
    if topic_arn:
        notify_sns(
            topic_arn=topic_arn,
            subject=f"[ZeroShift Agent] SCP refactor applied: {state.get('scp_name')}",
            message={
                "scpId": state.get("scp_id"),
                "scpName": state.get("scp_name"),
                "targetCount": state.get("target_count"),
                "agentExecutionId": rl.agent_execution_id,
            },
        )
    rl.log_step(node="finish_success", summary="SCP refactor applied and verified", next_node=None)
    return {**state, "outcome": "success"}


def finish_failure(state: ScpAgentState) -> ScpAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    rl.log_step(
        node="finish_failure",
        summary=f"SCP agent terminated in failure: {state.get('rollback_reason', 'no details')}",
        next_node=None,
    )
    return {**state, "outcome": state.get("outcome") or "failure"}


def build_graph():
    from langgraph.graph import END, StateGraph

    graph = StateGraph(ScpAgentState)
    graph.add_node("assess_ct_guard", assess_ct_guard)
    graph.add_node("snapshot_current", snapshot_current)
    graph.add_node("spot_check_equivalence", spot_check_equivalence)
    graph.add_node("assess_blast_radius", assess_blast_radius)
    graph.add_node("deliberate", deliberate)
    graph.add_node("apply_scp", apply_scp)
    graph.add_node("verify_scp", verify_scp)
    graph.add_node("rollback_scp", rollback_scp)
    graph.add_node("finish_success", finish_success)
    graph.add_node("finish_failure", finish_failure)

    graph.set_entry_point("assess_ct_guard")

    def route_from_ct(state: ScpAgentState) -> str:
        return "finish_failure" if state.get("control_tower_managed") else "snapshot_current"

    def route_from_deliberate(state: ScpAgentState) -> str:
        return "apply_scp" if not state.get("outcome") else "finish_failure"

    def route_from_apply(state: ScpAgentState) -> str:
        return "verify_scp" if state.get("applied") else "finish_failure"

    def route_from_verify(state: ScpAgentState) -> str:
        return "finish_success" if state.get("outcome") == "success" else "rollback_scp"

    graph.add_conditional_edges("assess_ct_guard", route_from_ct)
    graph.add_edge("snapshot_current", "spot_check_equivalence")
    graph.add_edge("spot_check_equivalence", "assess_blast_radius")
    graph.add_edge("assess_blast_radius", "deliberate")
    graph.add_conditional_edges("deliberate", route_from_deliberate)
    graph.add_conditional_edges("apply_scp", route_from_apply)
    graph.add_conditional_edges("verify_scp", route_from_verify)
    graph.add_edge("rollback_scp", "finish_failure")
    graph.add_edge("finish_success", END)
    graph.add_edge("finish_failure", END)

    return graph.compile()
