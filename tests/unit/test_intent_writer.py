from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from part1.code_analyzer.intent_writer import apply_diff, diff


@pytest.fixture
def intent_table(monkeypatch):
    monkeypatch.setenv("ZEROSHIFT_INTENT_TABLE", "zeroshift-intent-registry")
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


def test_diff_flags_new_actions_and_removals(intent_table):
    role_arn = "arn:aws:iam::0:role/foo"
    # Pre-populate the registry: s3:GetObject was previously active.
    intent_table.Table("zeroshift-intent-registry").put_item(
        Item={
            "roleArn": role_arn,
            "permission#version": "s3:GetObject#1",
            "permission": "s3:GetObject",
            "resource": "*",
            "status": "ACTIVE",
            "source": "PART1_CODE_ANALYSIS",
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
        }
    )

    delta = diff(role_arn=role_arn, inferred_actions=["s3:PutObject", "ses:SendEmail"])

    assert set(delta["toAdd"]) == {"s3:PutObject", "ses:SendEmail"}
    assert set(delta["toMarkPendingRemoval"]) == {"s3:GetObject"}
    assert delta["unchanged"] == []


def test_apply_writes_active_and_pending_rows(intent_table):
    role_arn = "arn:aws:iam::0:role/bar"
    delta = {
        "toAdd": ["s3:PutObject"],
        "toMarkPendingRemoval": ["ses:SendEmail"],
    }
    result = apply_diff(role_arn=role_arn, delta=delta, commit_hash="abc123", grace_days=5)
    assert result["activeAdded"] == 1
    assert result["pendingRemovalMarked"] == 1

    items = intent_table.Table("zeroshift-intent-registry").query(
        KeyConditionExpression="roleArn = :ra",
        ExpressionAttributeValues={":ra": role_arn},
    )["Items"]
    by_status = {i["status"]: i for i in items}
    assert by_status["ACTIVE"]["permission"] == "s3:PutObject"
    assert by_status["ACTIVE"]["addedAtCommit"] == "abc123"
    assert by_status["PENDING_REMOVAL"]["permission"] == "ses:SendEmail"
    # Grace expiry should be ~5 days in the future.
    expiry = datetime.fromisoformat(by_status["PENDING_REMOVAL"]["gracePeriodExpiry"])
    delta_seconds = (expiry - datetime.now(timezone.utc)).total_seconds()
    assert 4 * 24 * 3600 <= delta_seconds <= 6 * 24 * 3600


def test_diff_empty_registry(intent_table):
    delta = diff(role_arn="arn:aws:iam::0:role/new", inferred_actions=["s3:PutObject"])
    assert delta["toAdd"] == ["s3:PutObject"]
    assert delta["toMarkPendingRemoval"] == []
    assert delta["unchanged"] == []
