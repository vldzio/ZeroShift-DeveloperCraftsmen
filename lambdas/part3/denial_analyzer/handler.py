"""Lambda entry point for the SCP Denial Root-Cause Analyzer."""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

from part3.denial_analyzer import explanation_generator
from part3.denial_analyzer.policy_simulator import DenialContext, evaluate
from part3.denial_analyzer.scp_traverser import traverse
from shared.dynamodb_helpers import put_denial_analysis
from shared.logging_config import get_logger, new_correlation_id
from shared.organizations_client import OrganizationsClient

log = get_logger(__name__)

SNS_TOPIC_ARN_ENV = "DENIAL_ALERTS_TOPIC_ARN"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway HTTP API entry point.

    Accepts either:
      - ``{"eventId": "..."}`` -> look up CloudTrail event (not yet implemented at v1)
      - ``{"event": {<CloudTrail JSON>}}`` -> analyze the supplied event
      - Raw CloudTrail JSON (when invoked directly, e.g. via ``aws lambda invoke``)
    """
    correlation_id = new_correlation_id()
    log.info("denial_analyzer_start", extra={"awsRequestId": getattr(context, "aws_request_id", None)})

    body = _extract_body(event)
    cloudtrail_event = _resolve_cloudtrail_event(body)

    denial = _build_denial_context(cloudtrail_event)
    log.info(
        "denial_context",
        extra={
            "action": denial.action,
            "principal": denial.principal_arn,
            "region": denial.region,
            "accountId": denial.account_id,
        },
    )

    org_client = OrganizationsClient()
    scps = traverse(denial.account_id, org_client)
    log.info("scps_traversed", extra={"count": len(scps)})

    # Evaluate bottom-up (Account first). SCP semantics: any explicit Deny in
    # the tree denies the request; we surface the first one we can attribute
    # in bottom-up order because it is the most specific.
    denying_scp = None
    denying_decision = None
    ct_managed_in_path: list[dict[str, str]] = []

    for scp_along_path in reversed(scps):
        if scp_along_path.control_tower_managed:
            ct_managed_in_path.append(
                {"scpId": scp_along_path.policy.policy_id, "scpName": scp_along_path.policy.name}
            )
        decision = evaluate(
            scp_id=scp_along_path.policy.policy_id,
            scp_name=scp_along_path.policy.name,
            document=scp_along_path.policy.document,
            context=denial,
        )
        if decision is not None:
            denying_scp = scp_along_path
            denying_decision = decision
            break

    if denying_scp is None or denying_decision is None:
        log.info("no_denying_scp_found")
        return _api_response(
            {
                "analysisId": correlation_id,
                "found": False,
                "message": "No SCP in the org hierarchy matched the denied action; the denial may be caused by an identity-based policy, permissions boundary, session policy, or resource-based policy.",
                "controlTowerManagedInPath": ct_managed_in_path,
            }
        )

    explanation = explanation_generator.explain(
        scp_name=denying_scp.policy.name,
        scp_id=denying_scp.policy.policy_id,
        statement_id=denying_decision.statement_id,
        scp_document=denying_scp.policy.document,
        action=denial.action,
        resource=denial.resource_arn,
        principal=denial.principal_arn,
        region=denial.region,
        control_tower_managed=denying_scp.control_tower_managed,
    )

    _publish_to_sns(
        {
            "analysisId": correlation_id,
            "scp": {"id": denying_scp.policy.policy_id, "name": denying_scp.policy.name},
            "statementId": denying_decision.statement_id,
            "action": denial.action,
            "resource": denial.resource_arn,
            "principal": denial.principal_arn,
            "explanation": explanation,
            "controlTowerManaged": denying_scp.control_tower_managed,
        }
    )

    put_denial_analysis(
        scp_id=denying_scp.policy.policy_id,
        scp_name=denying_scp.policy.name,
        statement_id=denying_decision.statement_id,
        action=denial.action,
        resource=denial.resource_arn,
        principal=denial.principal_arn,
        account_id=denial.account_id,
        explanation=explanation,
        correlation_id=correlation_id,
        input_event_reference={"eventId": cloudtrail_event.get("eventID")},
        ct_managed=denying_scp.control_tower_managed,
    )

    return _api_response(
        {
            "analysisId": correlation_id,
            "found": True,
            "scp": {"id": denying_scp.policy.policy_id, "name": denying_scp.policy.name},
            "statementId": denying_decision.statement_id,
            "attachedAt": {
                "id": denying_scp.attached_at_node.node_id,
                "type": denying_scp.attached_at_node.node_type,
                "name": denying_scp.attached_at_node.name,
            },
            "explanation": explanation,
            "controlTowerManaged": denying_scp.control_tower_managed,
            "controlTowerManagedInPath": ct_managed_in_path,
        }
    )


# ---- helpers ----

def _extract_body(event: dict[str, Any]) -> dict[str, Any]:
    """API Gateway HTTP API wraps the client body in ``event['body']`` as a string.
    Direct Lambda invokes pass the payload as the whole event."""
    if "body" in event and isinstance(event["body"], str):
        try:
            return json.loads(event["body"])
        except json.JSONDecodeError:
            return {}
    return event


def _resolve_cloudtrail_event(body: dict[str, Any]) -> dict[str, Any]:
    if "event" in body and isinstance(body["event"], dict):
        return body["event"]
    if "eventId" in body:
        raise NotImplementedError(
            "CloudTrail Lake eventId lookup is not implemented at v1. "
            "Pass the raw event under the 'event' key."
        )
    # Treat the body itself as a CloudTrail event.
    return body


def _build_denial_context(cloudtrail_event: dict[str, Any]) -> DenialContext:
    event_source = cloudtrail_event.get("eventSource", "")
    event_name = cloudtrail_event.get("eventName", "")
    service = event_source.split(".")[0] if event_source else ""
    action = f"{service}:{event_name}" if service and event_name else event_name

    resources = cloudtrail_event.get("resources") or []
    resource_arn = ""
    if resources:
        resource_arn = resources[0].get("ARN", "")
    else:
        # Fall back to constructing an ARN from requestParameters where possible.
        params = cloudtrail_event.get("requestParameters") or {}
        if service == "s3" and params.get("bucketName"):
            key = params.get("key", "")
            resource_arn = f"arn:aws:s3:::{params['bucketName']}/{key}" if key else f"arn:aws:s3:::{params['bucketName']}"

    user_identity = cloudtrail_event.get("userIdentity") or {}
    principal_arn = user_identity.get("arn", "")
    account_id = (
        user_identity.get("accountId")
        or cloudtrail_event.get("recipientAccountId")
        or ""
    )
    region = cloudtrail_event.get("awsRegion", "")

    request_params = cloudtrail_event.get("requestParameters") or {}
    condition_keys: dict[str, str] = {
        "aws:PrincipalArn": principal_arn,
        "aws:RequestedRegion": region,
    }
    if request_params.get("instanceType"):
        condition_keys["ec2:InstanceType"] = request_params["instanceType"]
    if request_params.get("policyArn"):
        condition_keys["iam:PolicyARN"] = request_params["policyArn"]

    return DenialContext(
        action=action,
        resource_arn=resource_arn,
        principal_arn=principal_arn,
        account_id=account_id,
        region=region,
        condition_keys=condition_keys,
    )


def _publish_to_sns(message: dict[str, Any]) -> None:
    topic_arn = os.environ.get(SNS_TOPIC_ARN_ENV)
    if not topic_arn:
        log.warning("sns_topic_not_configured")
        return
    sns = boto3.client("sns")
    sns.publish(
        TopicArn=topic_arn,
        Subject=f"[ZeroShift] SCP denial: {message['scp']['name']}",
        Message=json.dumps(message, indent=2, default=str),
    )


def _api_response(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }
