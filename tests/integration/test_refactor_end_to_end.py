"""End-to-end fixture-mode test for the SCP refactor Step Functions handler.

Exercises every task in the state machine (load_scp, check_control_tower,
check_size, propose_refactor, verify_equivalence, create_change_request) with
Bedrock stubbed and SSM handled by fixture-mode (synthetic change request id).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def aws_and_env(monkeypatch):
    monkeypatch.setenv("ZEROSHIFT_INTENT_TABLE", "zeroshift-intent-registry")
    monkeypatch.setenv("ZEROSHIFT_SCP_AUDIT_TABLE", "zeroshift-scp-audit")
    monkeypatch.setenv("ZEROSHIFT_FIXTURE_MODE", "true")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="zeroshift-scp-audit",
            KeySchema=[
                {"AttributeName": "scpId", "KeyType": "HASH"},
                {"AttributeName": "changeId#timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "scpId", "AttributeType": "S"},
                {"AttributeName": "changeId#timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb


class _FakeCtx:
    aws_request_id = "test-req"
    function_name = "zeroshift-part3-refactorer"


def _fake_llm(**kwargs):
    from shared.llm_client import LlmResponse
    # Return the same document as "compressed" to guarantee equivalence.
    original_document_hint = kwargs.get("prompt", "")
    # Try to extract the input document JSON from the prompt for round-trip
    # equivalence. If parsing fails, fall back to a trivial equivalent doc.
    try:
        marker = "Original document:\n"
        start = original_document_hint.index(marker) + len(marker)
        end = original_document_hint.index("Rules you MUST follow:", start)
        doc = json.loads(original_document_hint[start:end].strip())
    except Exception:
        doc = {"Version": "2012-10-17", "Statement": []}
    return LlmResponse(
        output={
            "compressed_document": doc,
            "statements_removed_or_merged": 30,
            "new_size_bytes": len(json.dumps(doc)),
            "must_split": False,
            "compression_notes": "Round-trip identical document for equivalence-guaranteed test.",
        },
        raw={"stubbed": True},
        model_id="moonshotai.kimi-k2.5",
        provider="bedrock",
    )


def test_state_machine_tasks_run_end_to_end(aws_and_env):
    from part3.refactorer.handler import handler

    load_result = handler({"task": "load_scp", "scpId": "p-prod-oversized-deny"}, _FakeCtx())
    assert load_result["scpId"] == "p-prod-oversized-deny"
    assert load_result["originalSize"] > 8192
    assert load_result["controlTowerManaged"] is False

    ct_result = handler(
        {
            "task": "check_control_tower",
            "scpId": load_result["scpId"],
            "scpName": load_result["scpName"],
            "controlTowerManaged": load_result["controlTowerManaged"],
        },
        _FakeCtx(),
    )
    assert ct_result["shortCircuit"] is False

    size_result = handler(
        {"task": "check_size", "originalSize": load_result["originalSize"]}, _FakeCtx()
    )
    assert size_result["shortCircuit"] is False
    assert size_result["overThreshold"] is True

    with patch("part3.refactorer.proposer.invoke_structured", side_effect=_fake_llm):
        proposal_result = handler(
            {
                "task": "propose_refactor",
                "scpId": load_result["scpId"],
                "scpName": load_result["scpName"],
                "document": load_result["document"],
            },
            _FakeCtx(),
        )
    assert "proposal" in proposal_result
    assert proposal_result["proposal"]["must_split"] is False

    verify_result = handler(
        {
            "task": "verify_equivalence",
            "scpId": load_result["scpId"],
            "document": load_result["document"],
            "proposal": proposal_result["proposal"],
        },
        _FakeCtx(),
    )
    assert verify_result["equivalence"]["isEquivalent"] is True
    assert verify_result["equivalence"]["totalActionsChecked"] > 0

    cr_result = handler(
        {
            "task": "create_change_request",
            "scpId": load_result["scpId"],
            "scpName": load_result["scpName"],
            "originalSize": load_result["originalSize"],
            "proposal": proposal_result["proposal"],
            "equivalence": verify_result["equivalence"],
        },
        _FakeCtx(),
    )
    cr = cr_result["changeRequest"]
    assert cr["changeTemplateName"] == "ZeroShiftScpMutationTwoApprover"
    assert cr["fixtureMode"] is True
    assert cr["changeRequestId"].startswith("cr-fixture-p-prod-oversized-deny-")


def test_control_tower_scp_short_circuits(aws_and_env):
    from part3.refactorer.handler import handler

    load_result = handler(
        {"task": "load_scp", "scpId": "p-aws-guardrails-deny-root-user"}, _FakeCtx()
    )
    assert load_result["controlTowerManaged"] is True

    ct_result = handler(
        {
            "task": "check_control_tower",
            "scpId": load_result["scpId"],
            "scpName": load_result["scpName"],
            "controlTowerManaged": True,
        },
        _FakeCtx(),
    )
    assert ct_result["shortCircuit"] is True
    assert ct_result["controlTowerManaged"] is True


def test_apply_runbook_records_approved_no_mutation(aws_and_env, monkeypatch):
    monkeypatch.delenv("APPROVAL_REQUESTS_TOPIC_ARN", raising=False)
    from part3.refactorer.apply_runbook import handler

    result = handler(
        {
            "ScpId": "p-prod-oversized-deny",
            "ScpName": "ProductionRestrictedActionsExpanded",
            "ProposedDocument": json.dumps({"Version": "2012-10-17", "Statement": []}),
        },
        _FakeCtx(),
    )
    assert result["status"] == "APPROVED_NO_MUTATION"

    ddb = aws_and_env
    items = ddb.Table("zeroshift-scp-audit").query(
        KeyConditionExpression="scpId = :s",
        ExpressionAttributeValues={":s": "p-prod-oversized-deny"},
    )["Items"]
    assert len(items) == 1
    assert items[0]["changeType"] == "REFACTOR_APPROVED_NO_MUTATION"
