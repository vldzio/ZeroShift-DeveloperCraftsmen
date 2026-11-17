"""In-process AWS sandbox (moto) for the public demo.

The real Lambdas read/write DynamoDB tables, publish to SNS and, in agent mode,
mutate an IAM managed policy. The demo has no AWS account, so this starts moto
for the life of the process (the same technique the repo's integration tests use)
and seeds it from ``fixtures/``. Everything resets when the container restarts.
Only AWS SDK calls are intercepted; HTTP calls to the LLM gateway are untouched.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("demo_api.sandbox")

ACCOUNT_ID = "000000000000"  # matches what the fixture-mode role client substitutes for ACCOUNT

_started = False


def _table(ddb, name: str, hash_key: str, range_key: str) -> None:
    ddb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": hash_key, "KeyType": "HASH"}, {"AttributeName": range_key, "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": hash_key, "AttributeType": "S"},
            {"AttributeName": range_key, "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def start() -> None:
    global _started
    if _started:
        return
    os.environ.setdefault("MOTO_ACCOUNT_ID", ACCOUNT_ID)
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "demo-sandbox")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "demo-sandbox")
    os.environ.setdefault("ZEROSHIFT_ACCOUNT_ID", ACCOUNT_ID)

    import boto3
    from moto import mock_aws

    mock = mock_aws()
    mock.start()

    os.environ["ZEROSHIFT_INTENT_TABLE"] = "zeroshift-intent-registry"
    os.environ["ZEROSHIFT_SCP_AUDIT_TABLE"] = "zeroshift-scp-audit"
    os.environ["ZEROSHIFT_AGENT_REASONING_TABLE"] = "zeroshift-agent-reasoning"

    ddb = boto3.resource("dynamodb")
    _table(ddb, "zeroshift-intent-registry", "roleArn", "permission#version")
    _table(ddb, "zeroshift-scp-audit", "scpId", "changeId#timestamp")
    _table(ddb, "zeroshift-agent-reasoning", "agentExecutionId", "stepId#timestamp")

    sns = boto3.client("sns")
    os.environ["DENIAL_ALERTS_TOPIC_ARN"] = sns.create_topic(Name="zeroshift-denial-alerts")["TopicArn"]
    os.environ["APPROVAL_REQUESTS_TOPIC_ARN"] = sns.create_topic(Name="zeroshift-approval-requests")["TopicArn"]

    seed = json.loads((ROOT / "fixtures" / "intent_registry_seed.json").read_text(encoding="utf-8"))
    table = ddb.Table("zeroshift-intent-registry")
    for entry in seed.get("entries", []):
        item = {k: v for k, v in entry.items() if v is not None}
        if isinstance(item.get("roleArn"), str):
            item["roleArn"] = item["roleArn"].replace("ACCOUNT", ACCOUNT_ID)
        table.put_item(Item=item)

    # The one "real" managed policy the IAM remediation agent may rewrite.
    policy_doc = json.loads((ROOT / "fixtures" / "role_policies" / "zeroshift-demo-app-policy.json").read_text(encoding="utf-8"))
    boto3.client("iam").create_policy(PolicyName="zeroshift-demo-app-policy", PolicyDocument=json.dumps(policy_doc))

    _started = True
    log.info("sandbox started")
