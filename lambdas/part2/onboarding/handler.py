"""Lambda entry point for /discover-roles and /onboard-roles.

The API Gateway routes both paths to this single Lambda; ``event.rawPath``
selects the operation.
"""
from __future__ import annotations

import json
import os
from typing import Any

from part2.onboarding.baseliner import baseline
from shared.iam_role_client import (
    ManagedRole,
    get_role_by_arn,
    list_all_roles_for_discovery,
    put_role_permissions_boundary,
    tag_role,
)
from shared.logging_config import get_logger, new_correlation_id

log = get_logger(__name__)

PLATFORM_BOUNDARY_ARN_ENV = "ZEROSHIFT_PLATFORM_BOUNDARY_ARN"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    correlation_id = new_correlation_id()
    path = event.get("rawPath") or event.get("path") or ""

    if path.endswith("/discover-roles"):
        return _discover(event, correlation_id)
    if path.endswith("/onboard-roles"):
        return _onboard(event, correlation_id)

    return _response(400, {"error": f"unknown path: {path}"})


def _discover(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    roles = list_all_roles_for_discovery()
    log.info("discover_roles_done", extra={"count": len(roles), "correlationId": correlation_id})
    return _response(200, {"correlationId": correlation_id, "roles": roles})


def _onboard(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    body = _extract_body(event)
    role_arns: list[str] = body.get("roleArns") or []
    environment: str = body.get("environment") or "non-prod"
    if not role_arns:
        return _response(400, {"error": "roleArns list is required"})

    boundary_arn = os.environ.get(PLATFORM_BOUNDARY_ARN_ENV, "")
    results = []
    for role_arn in role_arns:
        role = get_role_by_arn(role_arn) or _synthesize_placeholder(role_arn)
        role_name = role.role_name

        tag_role(role_name, {"ManagedBy": "ZeroShift", "Environment": environment})
        if boundary_arn:
            put_role_permissions_boundary(role_name, boundary_arn)

        # Update the in-memory role's environment so baseline picks up the new tag.
        role.environment = environment
        role.tags = {"ManagedBy": "ZeroShift", "Environment": environment}

        base_result = baseline(role)
        results.append({"roleArn": role_arn, **base_result})

    log.info("onboard_roles_done", extra={"count": len(results), "correlationId": correlation_id})
    return _response(200, {"correlationId": correlation_id, "onboarded": results})


def _synthesize_placeholder(role_arn: str) -> ManagedRole:
    role_name = role_arn.rsplit("/", 1)[-1]
    return ManagedRole(
        role_arn=role_arn,
        role_name=role_name,
        environment="non-prod",
        attached_policy_id=f"{role_name}-inline",
        attached_policy_document={"Version": "2012-10-17", "Statement": []},
        attached_policy_arn=None,
        is_real_role=False,
        tags={},
    )


def _extract_body(event: dict[str, Any]) -> dict[str, Any]:
    if "body" in event and isinstance(event["body"], str):
        try:
            return json.loads(event["body"])
        except json.JSONDecodeError:
            return {}
    return event


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }
