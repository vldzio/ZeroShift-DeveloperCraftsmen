"""End-to-end fixture-mode test for the Part 1 code analyzer handler."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "code_samples"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ZEROSHIFT_INTENT_TABLE", "zeroshift-intent-registry")
    monkeypatch.setenv("ZEROSHIFT_FIXTURE_MODE", "true")
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
        yield ddb


class _Ctx:
    aws_request_id = "test-req"
    function_name = "zeroshift-part1-code-analyzer"


def _fake_augmenter(**kwargs):
    from shared.llm_client import LlmResponse

    return LlmResponse(
        output={"additional_actions": [], "confidence": "high", "notes": "no dynamic dispatch detected"},
        raw={"stubbed": True},
        model_id="moonshotai.kimi-k2.5",
        provider="bedrock",
    )


def test_analyze_code_dry_run(env):
    source = (FIXTURES / "news_summarizer.py").read_text(encoding="utf-8")
    with patch("part1.code_analyzer.llm_augmenter.invoke_structured", side_effect=_fake_augmenter):
        from part1.code_analyzer.handler import handler

        response = handler(
            {
                "body": json.dumps(
                    {
                        "code": source,
                        "targetRoleArn": "arn:aws:iam::0:role/news-summarizer",
                        "apply": False,
                    }
                )
            },
            _Ctx(),
        )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    actions = set(body["staticExtraction"]["actions"])
    assert "s3:GetObject" in actions
    assert "ses:SendEmail" in actions
    # Dry run — no rows written.
    assert body["applyResult"] is None
    assert set(body["delta"]["toAdd"]) >= {"s3:GetObject", "ses:SendEmail"}


def test_analyze_code_applies_intent_writes(env):
    source = (FIXTURES / "news_summarizer.py").read_text(encoding="utf-8")
    with patch("part1.code_analyzer.llm_augmenter.invoke_structured", side_effect=_fake_augmenter):
        from part1.code_analyzer.handler import handler

        response = handler(
            {
                "body": json.dumps(
                    {
                        "code": source,
                        "targetRoleArn": "arn:aws:iam::0:role/news-summarizer",
                        "commitHash": "abc123",
                        "apply": True,
                    }
                )
            },
            _Ctx(),
        )
    body = json.loads(response["body"])
    assert body["applyResult"] is not None
    assert body["applyResult"]["activeAdded"] >= 3

    table = env.Table("zeroshift-intent-registry")
    items = table.query(
        KeyConditionExpression="roleArn = :ra",
        ExpressionAttributeValues={":ra": "arn:aws:iam::0:role/news-summarizer"},
    )["Items"]
    active_actions = {i["permission"] for i in items if i["status"] == "ACTIVE"}
    assert "s3:GetObject" in active_actions
    assert "ses:SendEmail" in active_actions


def test_analyze_code_second_run_flags_removed_action(env):
    """After a first apply, an analysis with fewer SDK calls should
    surface a PENDING_REMOVAL for the missing action."""
    source_full = """import boto3
s3 = boto3.client("s3")
ses = boto3.client("ses")

def h():
    s3.get_object(Bucket="b", Key="k")
    ses.send_email(Source="a", Destination={}, Message={})
"""
    source_slim = """import boto3
s3 = boto3.client("s3")

def h():
    s3.get_object(Bucket="b", Key="k")
"""

    with patch("part1.code_analyzer.llm_augmenter.invoke_structured", side_effect=_fake_augmenter):
        from part1.code_analyzer.handler import handler

        handler(
            {"body": json.dumps({"code": source_full, "targetRoleArn": "arn:aws:iam::0:role/x", "apply": True})},
            _Ctx(),
        )
        second = handler(
            {"body": json.dumps({"code": source_slim, "targetRoleArn": "arn:aws:iam::0:role/x", "apply": False})},
            _Ctx(),
        )
    body = json.loads(second["body"])
    assert "ses:SendEmail" in body["delta"]["toMarkPendingRemoval"]


def test_missing_target_role_returns_400(env):
    from part1.code_analyzer.handler import handler

    resp = handler({"body": json.dumps({"code": "import boto3"})}, _Ctx())
    assert resp["statusCode"] == 400
