"""Apply the proposed IAM policy — the only mutation step in Part 2's workflow.

For a role that has a real ``attached_policy_arn`` (i.e., the CDK-deployed
demo role), calls ``iam:CreatePolicyVersion`` + ``iam:SetDefaultPolicyVersion``.
Fixture-only roles skip the AWS call and record the intended change.
"""
from __future__ import annotations

from typing import Any

from shared.iam_role_client import create_policy_version_and_set_default, get_role_by_arn
from shared.logging_config import get_logger

log = get_logger(__name__)


def apply(*, role_arn: str, proposed_policy: dict[str, Any]) -> dict[str, Any]:
    role = get_role_by_arn(role_arn)
    if not role:
        raise ValueError(f"Unknown managed role: {role_arn}")

    if not role.is_real_role or not role.attached_policy_arn:
        log.info(
            "apply_policy_noop_fixture_role",
            extra={"roleArn": role_arn, "reason": "not a real IAM role in this account"},
        )
        return {
            "applied": False,
            "reason": "fixture_role",
            "roleArn": role_arn,
            "policyArn": role.attached_policy_arn,
            "newVersionId": None,
        }

    new_version = create_policy_version_and_set_default(role.attached_policy_arn, proposed_policy)
    log.info(
        "apply_policy_applied",
        extra={"roleArn": role_arn, "policyArn": role.attached_policy_arn, "newVersionId": new_version},
    )
    return {
        "applied": True,
        "reason": "real_role",
        "roleArn": role_arn,
        "policyArn": role.attached_policy_arn,
        "newVersionId": new_version,
    }
