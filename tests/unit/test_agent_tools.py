"""Unit tests for the boto3-backed agent tools."""
from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def iam_mocks():
    with mock_aws():
        iam = boto3.client("iam", region_name="us-east-1")
        # Create a managed policy the tools can mutate.
        policy_doc = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "*"}
            ],
        }
        resp = iam.create_policy(
            PolicyName="test-managed",
            PolicyDocument=json.dumps(policy_doc),
        )
        yield {"iam": iam, "policy_arn": resp["Policy"]["Arn"]}


def test_simulate_iam_action_returns_decision(iam_mocks):
    """moto does not implement simulate_custom_policy; stub the boto3 call."""
    from unittest.mock import patch

    fake_client = type(
        "F",
        (),
        {
            "simulate_custom_policy": lambda self, **kw: {
                "EvaluationResults": [
                    {"EvalDecision": "allowed", "MatchedStatements": []}
                ]
            }
        },
    )()

    with patch("agents.common.tools.boto3.client", return_value=fake_client):
        from agents.common.tools import simulate_iam_action

        result = simulate_iam_action(
            action="s3:GetObject",
            policy_document={
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}],
            },
        )
    assert result["action"] == "s3:GetObject"
    assert result["decision"] == "allowed"


def test_create_and_set_default_policy_version(iam_mocks):
    from agents.common.tools import (
        create_new_policy_version,
        list_policy_versions,
        get_policy_document,
    )

    new_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": "*"}
        ],
    }
    result = create_new_policy_version(iam_mocks["policy_arn"], new_doc)
    assert result["new_version_id"]
    assert result["set_as_default"] is True

    versions = list_policy_versions(iam_mocks["policy_arn"])
    default = [v for v in versions["versions"] if v["is_default"]]
    assert len(default) == 1

    fetched = get_policy_document(iam_mocks["policy_arn"], result["new_version_id"])
    assert fetched["document"] == new_doc


def test_delete_policy_version(iam_mocks):
    """Delete a non-default version. Create two new versions (each becomes
    default in turn), then delete the first — which is now non-default."""
    from agents.common.tools import (
        create_new_policy_version,
        delete_policy_version,
        list_policy_versions,
    )

    doc_a = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "s3:PutObject", "Resource": "*"}],
    }
    doc_b = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}],
    }
    version_a = create_new_policy_version(iam_mocks["policy_arn"], doc_a)["new_version_id"]
    create_new_policy_version(iam_mocks["policy_arn"], doc_b)
    # version_a is now non-default; deleting it is allowed.
    delete_policy_version(iam_mocks["policy_arn"], version_a)
    versions = list_policy_versions(iam_mocks["policy_arn"])
    ids = [v["version_id"] for v in versions["versions"]]
    assert version_a not in ids


def test_notify_sns_publishes(monkeypatch):
    with mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        topic = sns.create_topic(Name="test-topic")

        from agents.common.tools import notify_sns

        result = notify_sns(
            topic_arn=topic["TopicArn"],
            subject="hi",
            message={"key": "value"},
        )
        assert result["ok"] is True
