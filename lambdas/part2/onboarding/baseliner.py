"""Baseline a newly-onboarded IAM role.

Snapshots the current attached policy to S3 and writes intent-registry entries
for every current permission so the first drift scan doesn't mis-flag every
permission as "unused".
"""
from __future__ import annotations

import os
from typing import Any

from shared.dynamodb_helpers import put_intent
from shared.iam_role_client import (
    ManagedRole,
    snapshot_policy_to_s3,
)
from shared.logging_config import get_logger

log = get_logger(__name__)

ARTIFACTS_BUCKET_ENV = "ZEROSHIFT_ARTIFACTS_BUCKET"


def baseline(role: ManagedRole) -> dict[str, Any]:
    bucket = os.environ.get(ARTIFACTS_BUCKET_ENV)
    snapshot_key = None
    if bucket:
        snapshot_key = snapshot_policy_to_s3(
            role_arn=role.role_arn,
            policy_document=role.attached_policy_document,
            bucket=bucket,
        )
        log.info("baseline_snapshot_written", extra={"roleArn": role.role_arn, "s3Key": snapshot_key})

    baselined = 0
    for stmt in role.attached_policy_document.get("Statement", []) or []:
        if stmt.get("Effect") != "Allow":
            continue
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        resources = stmt.get("Resource", "*")
        if isinstance(resources, str):
            resources = [resources]
        resource_str = ", ".join(resources) if resources else "*"
        for action in actions:
            try:
                put_intent(
                    role_arn=role.role_arn,
                    permission=action,
                    resource=resource_str,
                    status="ACTIVE",
                    added_at_commit="BASELINE",
                    risk_score="LOW",
                    source="PART2_BASELINE_ONBOARDING",
                    version=1,
                )
                baselined += 1
            except Exception as exc:
                log.warning("baseline_put_intent_failed", extra={"action": action, "error": str(exc)})

    return {
        "roleArn": role.role_arn,
        "snapshotS3Key": snapshot_key,
        "baselinedPermissionCount": baselined,
    }
