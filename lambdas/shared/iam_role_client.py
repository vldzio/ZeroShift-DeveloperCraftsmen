"""IAM role client with fixture-mode fallback.

Used by Part 2 (Stay Right) for reading managed IAM roles, snapshotting their
attached policies, and — in real mode against the CDK-deployed demo role —
creating new policy versions and setting them as default.

Fixture mode resolves everything from ``fixtures/managed_roles.json`` and
``fixtures/role_policies/*.json``. Real mode dispatches to boto3 IAM.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import boto3

from .logging_config import get_logger

log = get_logger(__name__)

MANAGED_BY_TAG = "ManagedBy"
MANAGED_BY_VALUE = "ZeroShift"
ENVIRONMENT_TAG = "Environment"


def _fixtures_dir() -> Path:
    override = os.environ.get("ZEROSHIFT_FIXTURES_DIR")
    if override:
        return Path(override)
    packaged = Path("/var/task/fixtures")
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[2] / "fixtures"


def _is_fixture_mode() -> bool:
    return os.environ.get("ZEROSHIFT_FIXTURE_MODE", "true").lower() == "true"


@dataclass
class ManagedRole:
    role_arn: str
    role_name: str
    environment: str
    attached_policy_id: str
    attached_policy_document: dict[str, Any]
    attached_policy_arn: str | None = None
    is_real_role: bool = False
    tags: dict[str, str] = field(default_factory=dict)


@lru_cache(maxsize=1)
def _load_managed_roles_manifest() -> dict[str, Any]:
    path = _fixtures_dir() / "managed_roles.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=32)
def _load_role_policy(policy_id: str) -> dict[str, Any]:
    path = _fixtures_dir() / "role_policies" / f"{policy_id}.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _current_account_id() -> str:
    override = os.environ.get("ZEROSHIFT_ACCOUNT_ID")
    if override:
        return override
    if _is_fixture_mode():
        # For fixture mode without an override, use a deterministic placeholder
        # so ARN string matching in tests is stable.
        return "000000000000"
    return boto3.client("sts").get_caller_identity()["Account"]


def _rewrite_account_placeholders(arn: str) -> str:
    return arn.replace("ACCOUNT", _current_account_id())


def list_managed_roles() -> list[ManagedRole]:
    """Return every IAM role currently under ZeroShift management.

    Fixture mode: reads managed_roles.json + role_policies/. Real mode:
    calls IAM to list all roles tagged ManagedBy=ZeroShift.
    """
    if _is_fixture_mode():
        return _list_from_fixtures()
    return _list_from_aws()


def _list_from_fixtures() -> list[ManagedRole]:
    manifest = _load_managed_roles_manifest()
    roles: list[ManagedRole] = []
    for entry in manifest.get("roles", []) or []:
        role_arn = _rewrite_account_placeholders(entry["roleArn"])
        attached_policy_arn = entry.get("attachedPolicyArn")
        if attached_policy_arn:
            attached_policy_arn = _rewrite_account_placeholders(attached_policy_arn)
        document = _load_role_policy(entry["attachedPolicyId"])
        roles.append(
            ManagedRole(
                role_arn=role_arn,
                role_name=entry["roleName"],
                environment=entry.get("environment", "non-prod"),
                attached_policy_id=entry["attachedPolicyId"],
                attached_policy_document=document,
                attached_policy_arn=attached_policy_arn,
                is_real_role=bool(entry.get("isRealRole")),
                tags={
                    MANAGED_BY_TAG: MANAGED_BY_VALUE,
                    ENVIRONMENT_TAG: entry.get("environment", "non-prod"),
                },
            )
        )
    return roles


def _list_from_aws() -> list[ManagedRole]:
    iam = boto3.client("iam")
    tagging = boto3.client("resourcegroupstaggingapi")
    resp = tagging.get_resources(
        ResourceTypeFilters=["iam:role"],
        TagFilters=[{"Key": MANAGED_BY_TAG, "Values": [MANAGED_BY_VALUE]}],
    )
    result: list[ManagedRole] = []
    for r in resp.get("ResourceTagMappingList", []) or []:
        role_arn = r["ResourceARN"]
        role_name = role_arn.rsplit("/", 1)[-1]
        tags = {t["Key"]: t["Value"] for t in r.get("Tags", []) or []}
        attached = iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
        if not attached:
            log.warning("managed_role_has_no_attached_policies", extra={"roleArn": role_arn})
            continue
        primary = attached[0]
        pol = iam.get_policy(PolicyArn=primary["PolicyArn"])["Policy"]
        version = iam.get_policy_version(
            PolicyArn=primary["PolicyArn"], VersionId=pol["DefaultVersionId"]
        )["PolicyVersion"]
        result.append(
            ManagedRole(
                role_arn=role_arn,
                role_name=role_name,
                environment=tags.get(ENVIRONMENT_TAG, "non-prod"),
                attached_policy_id=pol["PolicyName"],
                attached_policy_document=version["Document"] if isinstance(version["Document"], dict) else json.loads(version["Document"]),
                attached_policy_arn=primary["PolicyArn"],
                is_real_role=True,
                tags=tags,
            )
        )
    return result


def get_role_by_arn(role_arn: str) -> ManagedRole | None:
    for role in list_managed_roles():
        if role.role_arn == role_arn:
            return role
    return None


def list_all_roles_for_discovery() -> list[dict[str, Any]]:
    """List every IAM role in the account (for the onboarding wizard).

    Fixture mode returns a mocked set. Real mode calls ``iam:ListRoles``.
    """
    if _is_fixture_mode():
        manifest = _load_managed_roles_manifest()
        # Include the managed fixtures plus a couple of "unmanaged" roles to
        # exercise the onboarding wizard.
        result = []
        for entry in manifest.get("roles", []) or []:
            result.append(
                {
                    "roleArn": _rewrite_account_placeholders(entry["roleArn"]),
                    "roleName": entry["roleName"],
                    "tags": {MANAGED_BY_TAG: MANAGED_BY_VALUE, ENVIRONMENT_TAG: entry["environment"]},
                    "hasBoundary": True,
                    "isManaged": True,
                }
            )
        # Simulated unmanaged roles the customer might onboard next.
        for name, env in [("legacy-monolith-role", "prod"), ("api-gateway-lambda-role", "non-prod")]:
            result.append(
                {
                    "roleArn": f"arn:aws:iam::{_current_account_id()}:role/{name}",
                    "roleName": name,
                    "tags": {},
                    "hasBoundary": False,
                    "isManaged": False,
                }
            )
        return result

    iam = boto3.client("iam")
    result = []
    paginator = iam.get_paginator("list_roles")
    for page in paginator.paginate():
        for role in page.get("Roles", []) or []:
            tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=role["RoleName"]).get("Tags", [])}
            result.append(
                {
                    "roleArn": role["Arn"],
                    "roleName": role["RoleName"],
                    "tags": tags,
                    "hasBoundary": bool(role.get("PermissionsBoundary")),
                    "isManaged": tags.get(MANAGED_BY_TAG) == MANAGED_BY_VALUE,
                }
            )
    return result


def tag_role(role_name: str, tags: dict[str, str]) -> None:
    """Apply tags to a real IAM role. Fixture mode is a no-op."""
    if _is_fixture_mode():
        log.info("tag_role_fixture_mode", extra={"roleName": role_name, "tags": tags})
        return
    boto3.client("iam").tag_role(
        RoleName=role_name,
        Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
    )


def put_role_permissions_boundary(role_name: str, boundary_arn: str) -> None:
    if _is_fixture_mode():
        log.info(
            "put_boundary_fixture_mode",
            extra={"roleName": role_name, "boundaryArn": boundary_arn},
        )
        return
    boto3.client("iam").put_role_permissions_boundary(
        RoleName=role_name, PermissionsBoundary=boundary_arn
    )


def create_policy_version_and_set_default(
    policy_arn: str, new_document: dict[str, Any]
) -> str:
    """Create a new version of a customer-managed policy and set as default.

    Returns the new VersionId. Fixture mode is a no-op and returns ``"vFIXTURE"``.
    """
    if _is_fixture_mode():
        log.info("create_policy_version_fixture_mode", extra={"policyArn": policy_arn})
        return "vFIXTURE"
    iam = boto3.client("iam")
    # Only 5 versions allowed per policy; delete the oldest non-default if at limit.
    versions = iam.list_policy_versions(PolicyArn=policy_arn).get("Versions", [])
    non_default = [v for v in versions if not v["IsDefaultVersion"]]
    if len(versions) >= 5 and non_default:
        oldest = sorted(non_default, key=lambda v: v["CreateDate"])[0]
        iam.delete_policy_version(PolicyArn=policy_arn, VersionId=oldest["VersionId"])
    resp = iam.create_policy_version(
        PolicyArn=policy_arn,
        PolicyDocument=json.dumps(new_document),
        SetAsDefault=True,
    )
    return resp["PolicyVersion"]["VersionId"]


def snapshot_policy_to_s3(
    *,
    role_arn: str,
    policy_document: dict[str, Any],
    bucket: str,
    key_prefix: str = "policies",
) -> str:
    """Write a policy document snapshot to S3 for baseline / rollback purposes.

    Returns the S3 key. Fixture mode still writes (moto backs the S3 client)
    so the test suite can assert against it.
    """
    from datetime import datetime, timezone

    key = f"{key_prefix}/{role_arn.replace('/', '_')}/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(policy_document, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return key
