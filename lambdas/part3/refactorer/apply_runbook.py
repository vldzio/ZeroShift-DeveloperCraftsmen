"""Apply-runbook Lambda invoked by SSM Change Manager after two-approver approval.

Per user decision, v1 is safe-mode: this runbook does NOT call
``organizations:UpdatePolicy``. It records the approval in DynamoDB and
publishes an SNS notification. Real mutation is deferred to a later hardening
pass.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

from shared.logging_config import get_logger, new_correlation_id

log = get_logger(__name__)

AUDIT_TABLE_ENV = "ZEROSHIFT_SCP_AUDIT_TABLE"
APPROVAL_TOPIC_ENV = "APPROVAL_REQUESTS_TOPIC_ARN"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Runbook target invoked with parameters from the change request.

    Expected event shape (SSM passes runbook parameters as strings):
      {"ScpId": "...", "ScpName": "...", "ProposedDocument": "<json>"}
    """
    correlation_id = new_correlation_id()
    scp_id = event.get("ScpId") or ""
    scp_name = event.get("ScpName") or ""
    proposed_document_raw = event.get("ProposedDocument") or "{}"
    try:
        proposed_document = json.loads(proposed_document_raw)
    except json.JSONDecodeError:
        proposed_document = {}

    log.info(
        "apply_runbook_start",
        extra={"scpId": scp_id, "scpName": scp_name, "safeMode": True},
    )

    now = datetime.now(timezone.utc).isoformat()
    change_id = str(uuid.uuid4())

    audit_table_name = os.environ.get(AUDIT_TABLE_ENV)
    if audit_table_name:
        ddb = boto3.resource("dynamodb").Table(audit_table_name)
        ddb.put_item(
            Item={
                "scpId": scp_id,
                "changeId#timestamp": f"{change_id}#{now}",
                "changeType": "REFACTOR_APPROVED_NO_MUTATION",
                "scpName": scp_name,
                "proposedDocumentSize": len(json.dumps(proposed_document)),
                "correlationId": correlation_id,
                "createdAt": now,
                "note": "Safe-mode v1: two approvers accepted, but the SCP was NOT mutated. See zeroshift.md and Component 2 plan.",
            }
        )

    topic_arn = os.environ.get(APPROVAL_TOPIC_ENV)
    if topic_arn:
        boto3.client("sns").publish(
            TopicArn=topic_arn,
            Subject=f"[ZeroShift] SCP refactor approved (safe-mode, no mutation): {scp_name}",
            Message=json.dumps(
                {
                    "scpId": scp_id,
                    "scpName": scp_name,
                    "correlationId": correlation_id,
                    "changeId": change_id,
                    "safeMode": True,
                    "note": "Two approvers accepted the refactor. In v1 the SCP is NOT rewritten in Organizations. The proposal is recorded in DynamoDB for audit.",
                },
                indent=2,
            ),
        )

    log.info(
        "apply_runbook_done",
        extra={"scpId": scp_id, "changeId": change_id, "safeMode": True},
    )
    return {
        "status": "APPROVED_NO_MUTATION",
        "scpId": scp_id,
        "changeId": change_id,
        "correlationId": correlation_id,
    }
