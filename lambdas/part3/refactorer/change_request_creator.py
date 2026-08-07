"""Create an SSM Change Manager change request for an approved SCP refactor.

Wraps ``ssm:StartChangeRequestExecution`` referencing the two-approver
template registered by the CDK stack. Returns the resulting change request
identifier.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

from shared.logging_config import get_logger

log = get_logger(__name__)

CHANGE_TEMPLATE_NAME = os.environ.get(
    "ZEROSHIFT_CHANGE_TEMPLATE_NAME", "ZeroShiftScpMutationTwoApprover"
)


def create(
    *,
    scp_id: str,
    scp_name: str,
    original_size: int,
    proposed_document: dict[str, Any],
    equivalence_summary: dict[str, Any],
) -> dict[str, Any]:
    """Start a change request. Returns ``{changeRequestId, changeTemplateName, scheduledStartTime, ...}``.

    In fixture mode (``ZEROSHIFT_FIXTURE_MODE=true``) this does not call SSM
    and instead returns a synthetic change request identifier so the state
    machine can be exercised end-to-end without a live SSM Change Manager
    template.
    """
    fixture_mode = os.environ.get("ZEROSHIFT_FIXTURE_MODE", "true").lower() == "true"

    start_time = datetime.now(timezone.utc)
    end_time = start_time + timedelta(hours=1)

    payload_summary = {
        "scpId": scp_id,
        "scpName": scp_name,
        "originalSize": original_size,
        "proposedSize": len(json.dumps(proposed_document)),
        "totalActionsChecked": equivalence_summary.get("total_actions_checked"),
    }

    if fixture_mode:
        synthetic_id = f"cr-fixture-{scp_id}-{int(start_time.timestamp())}"
        log.info(
            "change_request_created_fixture_mode",
            extra={"changeRequestId": synthetic_id, "scpId": scp_id},
        )
        return {
            "changeRequestId": synthetic_id,
            "changeTemplateName": CHANGE_TEMPLATE_NAME,
            "scheduledStartTime": start_time.isoformat(),
            "scheduledEndTime": end_time.isoformat(),
            "fixtureMode": True,
            "payloadSummary": payload_summary,
        }

    ssm = boto3.client("ssm")
    resp = ssm.start_change_request_execution(
        DocumentName=CHANGE_TEMPLATE_NAME,
        ChangeRequestName=f"zeroshift-refactor-{scp_id}",
        ScheduledStartTime=start_time,
        ScheduledEndTime=end_time,
        Runbooks=[
            {
                "DocumentName": os.environ.get(
                    "ZEROSHIFT_APPLY_RUNBOOK_NAME", "ZeroShiftScpApplyRunbook"
                ),
                "DocumentVersion": "$DEFAULT",
                "Parameters": {
                    "ScpId": [scp_id],
                    "ScpName": [scp_name],
                    "ProposedDocument": [json.dumps(proposed_document)],
                },
            }
        ],
        AutoApprove=False,
    )
    log.info(
        "change_request_created",
        extra={
            "changeRequestId": resp.get("AutomationExecutionId"),
            "scpId": scp_id,
        },
    )
    return {
        "changeRequestId": resp.get("AutomationExecutionId"),
        "changeTemplateName": CHANGE_TEMPLATE_NAME,
        "scheduledStartTime": start_time.isoformat(),
        "scheduledEndTime": end_time.isoformat(),
        "fixtureMode": False,
        "payloadSummary": payload_summary,
    }
