"""End-to-end fixture-mode test for the IAM drift remediation state machine handler."""
from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("ZEROSHIFT_INTENT_TABLE", "zeroshift-intent-registry")
    monkeypatch.setenv("ZEROSHIFT_SCP_AUDIT_TABLE", "zeroshift-scp-audit")
    monkeypatch.setenv("ZEROSHIFT_FIXTURE_MODE", "true")
    monkeypatch.setenv("ZEROSHIFT_ACCOUNT_ID", "000000000000")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="zeroshift-intent-registry",
            KeySchema=[
                {"AttributeName": "roleArn", "KeyType": "HASH"},
                {"AttributeName": "permission#version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "roleArn", "AttributeType": "S"},
                {"AttributeName": "permission#version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
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


class _Ctx:
    aws_request_id = "test-req"
    function_name = "zeroshift-part2-remediator"


def _fake_llm(**kwargs):
    """Simulate Kimi's response for the demo-role fixture.

    The demo policy grants: s3:GetObject/PutObject/ListBucket/DeleteObject/
    DeleteBucket/PutBucketAcl + logs:CreateLogStream/PutLogEvents.
    In the counts fixture, s3:DeleteBucket and s3:PutBucketAcl have zero
    activity, so the analyzer asks for exactly those two to be removed.
    Everything else stays.
    """
    from shared.llm_client import LlmResponse

    return LlmResponse(
        output={
            "proposed_policy": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "S3AppData",
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:ListBucket",
                            "s3:DeleteObject",
                        ],
                        "Resource": [
                            "arn:aws:s3:::zeroshift-demo-app-*",
                            "arn:aws:s3:::zeroshift-demo-app-*/*",
                        ],
                    },
                    {
                        "Sid": "LoggingWrites",
                        "Effect": "Allow",
                        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                        "Resource": "*",
                    },
                ],
            },
            "removed_actions": ["s3:DeleteBucket", "s3:PutBucketAcl"],
            "rationale": "Removed unused destructive S3 actions with zero CloudTrail activity.",
            "warnings": [],
        },
        raw={"stubbed": True},
        model_id="moonshotai.kimi-k2.5",
        provider="bedrock",
    )


def test_low_risk_full_path_end_to_end(aws_env):
    with patch("part2.drift_detector.policy_proposer.invoke_structured", side_effect=_fake_llm):
        from part2.remediator.handler import handler

        role_arn = "arn:aws:iam::000000000000:role/zeroshift-demo-app-role"
        loaded = handler({"task": "load_role", "roleArn": role_arn}, _Ctx())
        assert loaded["roleName"] == "zeroshift-demo-app-role"
        assert loaded["isRealRole"] is True

        managed = handler({"task": "check_managed", "tags": loaded["tags"]}, _Ctx())
        assert managed["isManaged"] is True

        usage = handler({"task": "scan_usage", "roleArn": role_arn}, _Ctx())
        assert "s3:DeleteBucket" in usage["actionsToRemove"]
        assert usage["scannedActions"]

        intent = handler({"task": "read_intent_registry", "roleArn": role_arn}, _Ctx())
        assert "intentEntries" in intent

        proposed = handler(
            {
                "task": "propose_replacement",
                "roleArn": role_arn,
                "originalPolicy": loaded["originalPolicy"],
                "actionsToRemove": usage["actionsToRemove"],
            },
            _Ctx(),
        )
        assert "proposal" in proposed

        verified = handler(
            {
                "task": "simulate_equivalence",
                "originalPolicy": loaded["originalPolicy"],
                "proposal": proposed["proposal"],
                "actionsToRemove": usage["actionsToRemove"],
            },
            _Ctx(),
        )
        assert verified["equivalence"]["isEquivalent"] is True

        risk = handler(
            {
                "task": "score_risk",
                "environment": loaded["environment"],
                "actionsToRemove": usage["actionsToRemove"],
            },
            _Ctx(),
        )
        assert risk["riskTier"] == "LOW"

        # ApplyPolicy: fixture mode still writes a fixture no-op even for
        # `isRealRole=True` when running under fixture mode (the shared
        # iam_role_client short-circuits both branches).
        applied = handler(
            {
                "task": "apply_policy",
                "roleArn": role_arn,
                "proposal": proposed["proposal"],
            },
            _Ctx(),
        )
        assert "applyResult" in applied

        registry = handler(
            {
                "task": "update_intent_registry",
                "roleArn": role_arn,
                "actionsToRemove": usage["actionsToRemove"],
                "riskTier": risk["riskTier"],
                "applyResult": applied["applyResult"],
                "correlationId": "test-correlation",
            },
            _Ctx(),
        )
        assert registry["registryUpdated"] is True
        assert registry["removedCount"] == len(usage["actionsToRemove"])

        # Verify the registry rows exist.
        table = aws_env.Table("zeroshift-intent-registry")
        items = table.query(
            KeyConditionExpression="roleArn = :ra",
            ExpressionAttributeValues={":ra": role_arn},
        )["Items"]
        removed_actions = {i.get("permission") for i in items if i.get("status") == "REMOVED"}
        for action in usage["actionsToRemove"]:
            assert action in removed_actions


def test_high_risk_records_approval_required(aws_env):
    from part2.remediator.handler import handler

    role_arn = "arn:aws:iam::111111111111:role/payments-service-role"
    result = handler(
        {
            "task": "record_approval_required",
            "roleArn": role_arn,
            "actionsToRemove": ["iam:CreateAccessKey"],
            "riskTier": "HIGH",
            "correlationId": "cid-1",
        },
        _Ctx(),
    )
    assert result["approvalRequired"] is True

    audit = aws_env.Table("zeroshift-scp-audit").query(
        KeyConditionExpression="scpId = :s",
        ExpressionAttributeValues={":s": role_arn},
    )["Items"]
    assert any(i.get("changeType") == "DRIFT_APPROVAL_REQUIRED" for i in audit)


def test_unmanaged_role_short_circuits(aws_env):
    from part2.remediator.handler import handler

    result = handler({"task": "check_managed", "tags": {"Owner": "someone-else"}}, _Ctx())
    assert result["isManaged"] is False
    assert result["shortCircuit"] is True
