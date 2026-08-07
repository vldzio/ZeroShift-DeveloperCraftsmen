"""End-to-end fixture-mode test for the Stale-SCP Detector."""
from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def aws_and_env(monkeypatch):
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
        # Ensure no SNS topic is expected — the detector no-ops the publish
        # when APPROVAL_REQUESTS_TOPIC_ARN is unset.
        monkeypatch.delenv("APPROVAL_REQUESTS_TOPIC_ARN", raising=False)
        yield ddb


class _FakeCtx:
    aws_request_id = "test-req"
    function_name = "zeroshift-part3-stale-detector"


def test_scan_returns_findings_and_writes_audit_rows(aws_and_env):
    from part3.stale_detector.handler import handler

    resp = handler({"lookbackDays": 180}, _FakeCtx())
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])

    assert body["lookbackDays"] == 180
    assert body["scpsScanned"] > 0
    # At least one Deny statement in the fixture set has all-zero counts.
    assert body["findingCount"] >= 1

    # Every finding must correspond to a Deny statement, must list at least one
    # action, and must have zero total activity.
    for f in body["findings"]:
        assert f["statement_effect"] == "Deny"
        assert len(f["actions"]) >= 1
        assert f["total_activity"] == 0
        assert f["is_stale"] is True

    # Audit rows written to DynamoDB — one per finding.
    ddb = aws_and_env
    table = ddb.Table("zeroshift-scp-audit")
    written = 0
    for f in body["findings"]:
        items = table.query(
            KeyConditionExpression="scpId = :s",
            ExpressionAttributeValues={":s": f["scp_id"]},
        )["Items"]
        matching = [i for i in items if i.get("changeType") == "STALE_DENY_RECOMMENDATION"]
        assert matching, f"No audit row for scpId={f['scp_id']}"
        written += len(matching)
    assert written >= body["findingCount"]


def test_control_tower_scps_are_skipped(aws_and_env):
    from part3.stale_detector.handler import handler

    resp = handler({"lookbackDays": 180}, _FakeCtx())
    body = json.loads(resp["body"])

    # The CT-managed fixture (p-aws-guardrails-deny-root-user) must not appear
    # in findings, and the skip counter must be >= 1.
    assert body["scpsSkippedControlTower"] >= 1
    for f in body["findings"]:
        assert not f["scp_id"].startswith("p-aws-guardrails-")
        assert not f["scp_name"].startswith("aws-guardrails-")
