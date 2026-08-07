"""End-to-end fixture-mode test for the Denial Analyzer Lambda handler.

Runs the handler against each of the three demo fixtures with the LLM and
SNS calls stubbed. DynamoDB writes are captured against moto's in-memory
resource so the audit-trail assertion is real.
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
_DENIALS = _REPO_ROOT / "fixtures" / "denial_events"


@pytest.fixture
def aws_mocks(monkeypatch):
    monkeypatch.setenv("ZEROSHIFT_INTENT_TABLE", "zeroshift-intent-registry")
    monkeypatch.setenv("ZEROSHIFT_SCP_AUDIT_TABLE", "zeroshift-scp-audit")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for table_name in ("zeroshift-intent-registry", "zeroshift-scp-audit"):
            key_schema, attr_defs = _table_schema(table_name)
            ddb.create_table(
                TableName=table_name,
                KeySchema=key_schema,
                AttributeDefinitions=attr_defs,
                BillingMode="PAY_PER_REQUEST",
            )
        sns = boto3.client("sns", region_name="us-east-1")
        topic = sns.create_topic(Name="zeroshift-denial-alerts")
        monkeypatch.setenv("DENIAL_ALERTS_TOPIC_ARN", topic["TopicArn"])
        yield ddb, sns


def _table_schema(table_name: str):
    if table_name == "zeroshift-intent-registry":
        return (
            [
                {"AttributeName": "roleArn", "KeyType": "HASH"},
                {"AttributeName": "permission#version", "KeyType": "RANGE"},
            ],
            [
                {"AttributeName": "roleArn", "AttributeType": "S"},
                {"AttributeName": "permission#version", "AttributeType": "S"},
            ],
        )
    return (
        [
            {"AttributeName": "scpId", "KeyType": "HASH"},
            {"AttributeName": "changeId#timestamp", "KeyType": "RANGE"},
        ],
        [
            {"AttributeName": "scpId", "AttributeType": "S"},
            {"AttributeName": "changeId#timestamp", "AttributeType": "S"},
        ],
    )


def _stub_llm_response(*args, **kwargs):
    from shared.llm_client import LlmResponse

    return LlmResponse(
        output={
            "summary": "Access denied by an SCP that blocks operations outside approved regions.",
            "cause": "The Production OU has an SCP that denies non-IAM actions in any region other than us-east-1, us-west-2, and eu-west-1.",
            "suggested_fix": "Request that ap-south-1 be added to the approved regions list, or perform the operation in an approved region.",
            "affected_condition_keys": ["aws:RequestedRegion"],
        },
        raw={"stubbed": True},
        model_id="moonshotai.kimi-k2.5",
        provider="bedrock",
    )


@pytest.mark.parametrize(
    "fixture_name,expected_scp_name",
    [
        ("production-region-deny", "DenyNonApprovedRegions"),
        ("root-instance-type-deny", "DenyExpensiveInstances"),
        ("account-iam-wildcard-deny", "DenyIAMWildcards"),
    ],
)
def test_end_to_end_denial_attribution(aws_mocks, fixture_name, expected_scp_name):
    event = json.loads((_DENIALS / f"{fixture_name}.json").read_text(encoding="utf-8"))

    with patch("shared.llm_client.invoke_structured", side_effect=_stub_llm_response):
        from part3.denial_analyzer.handler import handler

        response = handler(event, _FakeLambdaContext())

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["found"] is True
    assert body["scp"]["name"] == expected_scp_name
    assert body["explanation"]["summary"]

    # Audit record written to DynamoDB
    ddb, _sns = aws_mocks
    table = ddb.Table("zeroshift-scp-audit")
    items = table.query(
        KeyConditionExpression="scpId = :s",
        ExpressionAttributeValues={":s": body["scp"]["id"]},
    )["Items"]
    assert len(items) == 1
    assert items[0]["scpName"] == expected_scp_name


def test_control_tower_managed_flagged_in_path(aws_mocks):
    event = json.loads((_DENIALS / "root-instance-type-deny.json").read_text(encoding="utf-8"))

    with patch("shared.llm_client.invoke_structured", side_effect=_stub_llm_response):
        from part3.denial_analyzer.handler import handler

        response = handler(event, _FakeLambdaContext())

    body = json.loads(response["body"])
    ct_in_path = body.get("controlTowerManagedInPath", [])
    ct_names = [item["scpName"] for item in ct_in_path]
    assert any(name.startswith("aws-guardrails-") for name in ct_names)


class _FakeLambdaContext:
    aws_request_id = "test-request-id"
    function_name = "zeroshift-part3-denial-analyzer"
