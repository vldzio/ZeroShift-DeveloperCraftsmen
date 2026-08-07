"""LangGraph state graph for the IAM Remediation Agent.

Autonomous apply for Part 2's LOW-risk drift remediation path. Kimi K2.5 on
Bedrock reasons at two decision points (``assess_proposal`` and ``verify``);
the intermediate nodes are deterministic tool calls whose outputs the LLM sees.
This gives the model real agency (it picks whether to apply or reject) while
keeping the mutation surface bounded.
"""
from __future__ import annotations

import json
import os
from typing import Any, TypedDict

from agents.common.bedrock_client import get_chat_model
from agents.common.reasoning_logger import ReasoningLogger
from agents.common.tools import (
    create_new_policy_version,
    delete_policy_version,
    get_policy_document,
    list_policy_versions,
    notify_sns,
    set_default_policy_version,
    simulate_iam_action,
)
from shared.logging_config import get_logger

log = get_logger(__name__)


class IamAgentState(TypedDict, total=False):
    role_arn: str
    policy_arn: str
    original_policy: dict[str, Any]
    proposed_policy: dict[str, Any]
    actions_to_remove: list[str]
    risk_tier: str
    simulator_results: list[dict[str, Any]]
    simulator_ok: bool
    previous_default_version_id: str
    new_version_id: str
    verified: bool
    rollback_reason: str
    outcome: str
    reasoning_logger: Any
    correlation_id: str


_ASSESS_PROMPT = """You are an autonomous IAM remediation agent. You are handed a proposed IAM policy
that removes a set of actions from a role's currently-attached customer-managed policy.

Your job: decide whether to (a) run simulations first, (b) directly apply the proposal,
or (c) reject the proposal.

Rules:
- If actions_to_remove is empty, REJECT with reason "no drift".
- If risk_tier is HIGH, REJECT with reason "high-risk requires human approval, not the LOW-tier agent".
- Otherwise, SIMULATE first — run per-action simulator checks before mutating IAM.

Respond with a JSON object matching this shape (no prose):
{{"decision": "SIMULATE" | "APPLY" | "REJECT", "rationale": "<one sentence>"}}"""


_VERIFY_PROMPT = """You just applied a new IAM policy version. The re-fetched document is below.
Compare its allowed action set to the pre-apply proposed_policy.

Rules:
- If the re-fetched document's Allow action set exactly matches the proposed_policy's Allow action set,
  return {{"verified": true, "rationale": "<one sentence>"}}.
- Otherwise return {{"verified": false, "rationale": "<what differs>"}}.

Do not add or remove actions. Only report what you observe."""


def _allow_action_set(document: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for stmt in document.get("Statement", []) or []:
        if stmt.get("Effect") != "Allow":
            continue
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        for a in actions:
            result.add(a)
    return result


def _decide_via_llm(prompt: str, context: dict[str, Any]) -> dict[str, Any]:
    """Call Kimi K2.5 with a structured prompt + JSON context, expect JSON back."""
    model = get_chat_model()
    full = f"{prompt}\n\nContext:\n{json.dumps(context, default=str, indent=2)}"
    resp = model.invoke(full)
    text = resp.content if hasattr(resp, "content") else str(resp)
    if isinstance(text, list):
        text = "".join(str(chunk.get("text", chunk)) if isinstance(chunk, dict) else str(chunk) for chunk in text)
    # Extract JSON from anywhere in the response.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {"decision": "REJECT", "rationale": f"LLM output was not parseable JSON: {text[:200]}"}


# ---- Nodes ----

def assess_proposal(state: IamAgentState) -> IamAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    context = {
        "role_arn": state.get("role_arn"),
        "policy_arn": state.get("policy_arn"),
        "risk_tier": state.get("risk_tier"),
        "actions_to_remove": state.get("actions_to_remove", []),
        "removed_count": len(state.get("actions_to_remove") or []),
    }
    decision = _decide_via_llm(_ASSESS_PROMPT, context)
    rl.log_step(
        node="assess_proposal",
        summary=f"LLM decision: {decision.get('decision')} — {decision.get('rationale', '')}",
        llm_output=decision,
        next_node="run_simulations" if decision.get("decision") == "SIMULATE" else "finish_failure",
    )
    if decision.get("decision") == "REJECT":
        return {**state, "outcome": "rejected", "rollback_reason": decision.get("rationale", "")}
    if decision.get("decision") == "APPLY":
        # Agent chose to skip simulation. Allow but log.
        return {**state, "simulator_ok": True, "outcome": ""}
    return {**state, "outcome": ""}


def run_simulations(state: IamAgentState) -> IamAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    results: list[dict[str, Any]] = []
    ok = True
    for action in state.get("actions_to_remove") or []:
        # Simulate against the ORIGINAL — should be Allow.
        orig = simulate_iam_action(action, state["original_policy"])
        # Simulate against the PROPOSED — should be implicitDeny.
        proposed = simulate_iam_action(action, state["proposed_policy"])
        result = {"action": action, "original": orig["decision"], "proposed": proposed["decision"]}
        results.append(result)
        # Expected: original allowed, proposed denied. Anything else is a divergence.
        if not (orig["decision"] == "allowed" and proposed["decision"] in ("implicitDeny", "explicitDeny")):
            ok = False
    rl.log_step(
        node="run_simulations",
        summary=f"Simulated {len(results)} action(s); equivalence ok={ok}",
        tool_name="simulate_iam_action",
        tool_result={"results": results, "ok": ok},
        next_node="apply" if ok else "finish_failure",
    )
    return {**state, "simulator_results": results, "simulator_ok": ok, "outcome": "" if ok else "rejected"}


def apply(state: IamAgentState) -> IamAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    versions = list_policy_versions(state["policy_arn"])
    prev_default = next((v["version_id"] for v in versions["versions"] if v["is_default"]), None)
    result = create_new_policy_version(state["policy_arn"], state["proposed_policy"])
    rl.log_step(
        node="apply",
        summary=f"Applied new version {result['new_version_id']} to {state['policy_arn']}",
        tool_name="create_new_policy_version",
        tool_args={"policy_arn": state["policy_arn"]},
        tool_result=result,
        next_node="verify",
    )
    return {
        **state,
        "previous_default_version_id": prev_default or "",
        "new_version_id": result["new_version_id"],
        "outcome": "",
    }


def verify(state: IamAgentState) -> IamAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    fetched = get_policy_document(state["policy_arn"], state["new_version_id"])
    proposed_allow = _allow_action_set(state["proposed_policy"])
    fetched_allow = _allow_action_set(fetched["document"])
    context = {
        "proposed_allow_actions": sorted(proposed_allow),
        "fetched_allow_actions": sorted(fetched_allow),
        "match": proposed_allow == fetched_allow,
    }
    llm_result = _decide_via_llm(_VERIFY_PROMPT, context)
    ok = bool(llm_result.get("verified"))
    rl.log_step(
        node="verify",
        summary=f"Verification: {ok} — {llm_result.get('rationale', '')}",
        tool_name="get_policy_document",
        tool_result=context,
        llm_output=llm_result,
        next_node="finish_success" if ok else "rollback",
    )
    return {
        **state,
        "verified": ok,
        "rollback_reason": "" if ok else llm_result.get("rationale", "verification failed"),
    }


def rollback(state: IamAgentState) -> IamAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    prev = state.get("previous_default_version_id")
    new_v = state.get("new_version_id")
    if prev:
        set_default_policy_version(state["policy_arn"], prev)
    if new_v:
        try:
            delete_policy_version(state["policy_arn"], new_v)
        except Exception as exc:
            log.warning("rollback_delete_failed", extra={"error": str(exc)})
    rl.log_step(
        node="rollback",
        summary=f"Rolled back to {prev}; reason: {state.get('rollback_reason', 'unknown')}",
        tool_name="set_default_policy_version + delete_policy_version",
        tool_result={"restored_version": prev, "deleted_version": new_v},
        next_node="finish_failure",
    )
    return {**state, "outcome": "failure"}


def finish_success(state: IamAgentState) -> IamAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    topic_arn = os.environ.get("APPROVAL_REQUESTS_TOPIC_ARN")
    if topic_arn:
        notify_sns(
            topic_arn=topic_arn,
            subject=f"[ZeroShift Agent] IAM remediation applied for {state.get('role_arn')}",
            message={
                "roleArn": state.get("role_arn"),
                "policyArn": state.get("policy_arn"),
                "newVersionId": state.get("new_version_id"),
                "actionsRemoved": state.get("actions_to_remove"),
                "agentExecutionId": rl.agent_execution_id,
            },
        )
    rl.log_step(node="finish_success", summary="Remediation applied and verified", next_node=None)
    return {**state, "outcome": "success"}


def finish_failure(state: IamAgentState) -> IamAgentState:
    rl: ReasoningLogger = state["reasoning_logger"]  # type: ignore[assignment]
    rl.log_step(
        node="finish_failure",
        summary=f"Agent terminated in failure: {state.get('rollback_reason', 'no details')}",
        next_node=None,
    )
    return {**state, "outcome": state.get("outcome") or "failure"}


# ---- Build the graph ----

def build_graph():
    from langgraph.graph import END, StateGraph

    graph = StateGraph(IamAgentState)
    graph.add_node("assess_proposal", assess_proposal)
    graph.add_node("run_simulations", run_simulations)
    graph.add_node("apply", apply)
    graph.add_node("verify", verify)
    graph.add_node("rollback", rollback)
    graph.add_node("finish_success", finish_success)
    graph.add_node("finish_failure", finish_failure)

    graph.set_entry_point("assess_proposal")

    def route_from_assess(state: IamAgentState) -> str:
        if state.get("outcome") == "rejected":
            return "finish_failure"
        if state.get("simulator_ok"):
            return "apply"
        return "run_simulations"

    def route_from_simulations(state: IamAgentState) -> str:
        return "apply" if state.get("simulator_ok") else "finish_failure"

    def route_from_verify(state: IamAgentState) -> str:
        return "finish_success" if state.get("verified") else "rollback"

    graph.add_conditional_edges("assess_proposal", route_from_assess)
    graph.add_conditional_edges("run_simulations", route_from_simulations)
    graph.add_edge("apply", "verify")
    graph.add_conditional_edges("verify", route_from_verify)
    graph.add_edge("rollback", "finish_failure")
    graph.add_edge("finish_success", END)
    graph.add_edge("finish_failure", END)

    return graph.compile()
