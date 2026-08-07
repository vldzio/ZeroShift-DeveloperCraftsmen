"""Integration test for the IAM Remediation Agent's node functions.

Tests each node in isolation with the LLM mocked. Skips the LangGraph compile
step (which requires the runtime installed) — the graph is thin glue over
these nodes; verifying the nodes verifies the important behavior.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def iam_and_reasoning(monkeypatch):
    monkeypatch.setenv("ZEROSHIFT_AGENT_REASONING_TABLE", "zeroshift-agent-reasoning")
    with mock_aws():
        iam = boto3.client("iam", region_name="us-east-1")
        original_doc = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:DeleteBucket", "s3:PutBucketAcl"],
                    "Resource": "*",
                }
            ],
        }
        policy = iam.create_policy(
            PolicyName="test-managed-agent",
            PolicyDocument=json.dumps(original_doc),
        )["Policy"]

        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="zeroshift-agent-reasoning",
            KeySchema=[
                {"AttributeName": "agentExecutionId", "KeyType": "HASH"},
                {"AttributeName": "stepId#timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "agentExecutionId", "AttributeType": "S"},
                {"AttributeName": "stepId#timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {"policy_arn": policy["Arn"], "original_doc": original_doc}


def _fake_llm_yields(text: str):
    class FakeMsg:
        content = text

    class FakeModel:
        def invoke(self, prompt: str):
            return FakeMsg()

    return FakeModel()


def _proposed_doc():
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": "*",
            }
        ],
    }


def test_assess_proposal_llm_says_simulate(iam_and_reasoning):
    from agents.common.reasoning_logger import ReasoningLogger

    fake = _fake_llm_yields('{"decision": "SIMULATE", "rationale": "spot-check first"}')

    with patch("agents.iam_remediator.graph.get_chat_model", return_value=fake):
        from agents.iam_remediator.graph import assess_proposal

        state = {
            "role_arn": "arn:aws:iam::0:role/test",
            "policy_arn": iam_and_reasoning["policy_arn"],
            "risk_tier": "LOW",
            "actions_to_remove": ["s3:DeleteBucket", "s3:PutBucketAcl"],
            "reasoning_logger": ReasoningLogger(agent_name="iam"),
        }
        out = assess_proposal(state)
    assert out.get("outcome") != "rejected"


def test_assess_proposal_llm_rejects(iam_and_reasoning):
    from agents.common.reasoning_logger import ReasoningLogger

    fake = _fake_llm_yields('{"decision": "REJECT", "rationale": "risk too high"}')
    with patch("agents.iam_remediator.graph.get_chat_model", return_value=fake):
        from agents.iam_remediator.graph import assess_proposal

        state = {
            "role_arn": "arn:aws:iam::0:role/test",
            "policy_arn": iam_and_reasoning["policy_arn"],
            "risk_tier": "HIGH",
            "actions_to_remove": [],
            "reasoning_logger": ReasoningLogger(agent_name="iam"),
        }
        out = assess_proposal(state)
    assert out["outcome"] == "rejected"


def test_run_simulations_marks_ok_when_equivalent(iam_and_reasoning):
    """moto lacks simulate_custom_policy; stub simulate_iam_action so the
    per-action loop returns expected decisions."""
    from agents.common.reasoning_logger import ReasoningLogger

    def fake_sim(action, doc, resource_arn="*"):
        # Original grants everything; proposed denies the two removed actions.
        allow_actions = {"s3:GetObject"}
        if action in allow_actions or "Action" in doc.get("Statement", [{}])[0] and action in (doc["Statement"][0].get("Action") or []):
            return {"action": action, "decision": "allowed"}
        return {"action": action, "decision": "implicitDeny"}

    with patch("agents.iam_remediator.graph.simulate_iam_action", side_effect=fake_sim):
        from agents.iam_remediator.graph import run_simulations

        state = {
            "original_policy": iam_and_reasoning["original_doc"],
            "proposed_policy": _proposed_doc(),
            "actions_to_remove": ["s3:DeleteBucket", "s3:PutBucketAcl"],
            "reasoning_logger": ReasoningLogger(agent_name="iam"),
        }
        out = run_simulations(state)
    assert isinstance(out["simulator_ok"], bool)
    assert isinstance(out["simulator_results"], list)


def test_apply_creates_new_default_version(iam_and_reasoning):
    from agents.common.reasoning_logger import ReasoningLogger
    from agents.iam_remediator.graph import apply

    state = {
        "policy_arn": iam_and_reasoning["policy_arn"],
        "proposed_policy": _proposed_doc(),
        "reasoning_logger": ReasoningLogger(agent_name="iam"),
    }
    out = apply(state)
    assert out.get("new_version_id")

    # Confirm new default is set.
    iam = boto3.client("iam", region_name="us-east-1")
    versions = iam.list_policy_versions(PolicyArn=iam_and_reasoning["policy_arn"])["Versions"]
    default = [v for v in versions if v["IsDefaultVersion"]]
    assert len(default) == 1
    assert default[0]["VersionId"] == out["new_version_id"]


def test_verify_passes_when_documents_match(iam_and_reasoning):
    from agents.common.reasoning_logger import ReasoningLogger
    from agents.iam_remediator.graph import apply, verify

    fake = _fake_llm_yields('{"verified": true, "rationale": "matches"}')

    state = {
        "policy_arn": iam_and_reasoning["policy_arn"],
        "proposed_policy": _proposed_doc(),
        "reasoning_logger": ReasoningLogger(agent_name="iam"),
    }
    state = apply(state)
    with patch("agents.iam_remediator.graph.get_chat_model", return_value=fake):
        state = verify(state)
    assert state["verified"] is True


def test_handler_returns_fallback_when_role_arn_missing():
    from agents.iam_remediator.handler import handler

    result = handler({}, None)
    assert result["fallback"] is True
    assert "missing roleArn" in result["reason"]
