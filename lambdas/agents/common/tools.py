"""boto3-backed tool functions the LangGraph agents call.

Each function is a plain callable the agent can invoke via ``@tool``-decorated
LangGraph adapters. Kept intentionally thin — the agent's job is orchestration
and reasoning; these functions do the raw AWS work.

Every function returns a JSON-serializable dict so the reasoning logger can
persist tool outputs verbatim.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from shared.logging_config import get_logger

log = get_logger(__name__)


# ============================================================================
# IAM tools (used by IAM Remediation Agent)
# ============================================================================

def simulate_iam_action(
    action: str, policy_document: dict[str, Any], resource_arn: str = "*"
) -> dict[str, Any]:
    """Simulate one IAM action against one policy document. Returns decision."""
    iam = boto3.client("iam")
    resp = iam.simulate_custom_policy(
        PolicyInputList=[json.dumps(policy_document)],
        ActionNames=[action],
        ResourceArns=[resource_arn],
    )
    results = resp.get("EvaluationResults", [])
    if not results:
        return {"action": action, "decision": "unknown"}
    return {
        "action": action,
        "decision": results[0].get("EvalDecision", "unknown"),
        "matched_statements": results[0].get("MatchedStatements", []),
    }


def list_policy_versions(policy_arn: str) -> dict[str, Any]:
    iam = boto3.client("iam")
    resp = iam.list_policy_versions(PolicyArn=policy_arn)
    return {
        "policy_arn": policy_arn,
        "versions": [
            {
                "version_id": v["VersionId"],
                "is_default": v["IsDefaultVersion"],
                "create_date": str(v["CreateDate"]),
            }
            for v in resp.get("Versions", [])
        ],
    }


def get_policy_document(policy_arn: str, version_id: str) -> dict[str, Any]:
    iam = boto3.client("iam")
    resp = iam.get_policy_version(PolicyArn=policy_arn, VersionId=version_id)
    document = resp["PolicyVersion"]["Document"]
    if isinstance(document, str):
        document = json.loads(document)
    return {"policy_arn": policy_arn, "version_id": version_id, "document": document}


def create_new_policy_version(
    policy_arn: str, document: dict[str, Any]
) -> dict[str, Any]:
    iam = boto3.client("iam")
    # Ensure we're under the 5-version limit; delete the oldest non-default.
    versions = iam.list_policy_versions(PolicyArn=policy_arn).get("Versions", [])
    if len(versions) >= 5:
        non_default = [v for v in versions if not v["IsDefaultVersion"]]
        if non_default:
            oldest = sorted(non_default, key=lambda v: v["CreateDate"])[0]
            iam.delete_policy_version(
                PolicyArn=policy_arn, VersionId=oldest["VersionId"]
            )
    resp = iam.create_policy_version(
        PolicyArn=policy_arn,
        PolicyDocument=json.dumps(document),
        SetAsDefault=True,
    )
    return {
        "policy_arn": policy_arn,
        "new_version_id": resp["PolicyVersion"]["VersionId"],
        "set_as_default": True,
    }


def set_default_policy_version(policy_arn: str, version_id: str) -> dict[str, Any]:
    iam = boto3.client("iam")
    iam.set_default_policy_version(PolicyArn=policy_arn, VersionId=version_id)
    return {"policy_arn": policy_arn, "default_version_id": version_id, "ok": True}


def delete_policy_version(policy_arn: str, version_id: str) -> dict[str, Any]:
    iam = boto3.client("iam")
    iam.delete_policy_version(PolicyArn=policy_arn, VersionId=version_id)
    return {"policy_arn": policy_arn, "deleted_version_id": version_id, "ok": True}


# ============================================================================
# Organizations / SCP tools (used by SCP Governance Agent)
# ============================================================================

def describe_scp(policy_id: str) -> dict[str, Any]:
    org = boto3.client("organizations")
    resp = org.describe_policy(PolicyId=policy_id)["Policy"]
    summary = resp["PolicySummary"]
    return {
        "policy_id": summary["Id"],
        "name": summary["Name"],
        "aws_managed": summary.get("AwsManaged", False),
        "document": json.loads(resp["Content"]),
    }


def list_targets_for_scp(policy_id: str) -> dict[str, Any]:
    org = boto3.client("organizations")
    resp = org.list_targets_for_policy(PolicyId=policy_id)
    return {
        "policy_id": policy_id,
        "targets": [
            {"target_id": t["TargetId"], "type": t["Type"], "name": t.get("Name", "")}
            for t in resp.get("Targets", [])
        ],
        "target_count": len(resp.get("Targets", [])),
    }


def is_control_tower_managed(policy_name: str) -> dict[str, Any]:
    from shared.control_tower_guard import is_ct_managed

    return {"policy_name": policy_name, "control_tower_managed": is_ct_managed(policy_name)}


def update_scp(policy_id: str, document: dict[str, Any]) -> dict[str, Any]:
    org = boto3.client("organizations")
    resp = org.update_policy(PolicyId=policy_id, Content=json.dumps(document))
    return {
        "policy_id": resp["Policy"]["PolicySummary"]["Id"],
        "name": resp["Policy"]["PolicySummary"]["Name"],
        "ok": True,
    }


# ============================================================================
# Notification tool (used by both agents)
# ============================================================================

def notify_sns(topic_arn: str, subject: str, message: dict[str, Any] | str) -> dict[str, Any]:
    sns = boto3.client("sns")
    payload = message if isinstance(message, str) else json.dumps(message, indent=2, default=str)
    sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=payload)
    return {"topic_arn": topic_arn, "subject": subject[:100], "ok": True}
