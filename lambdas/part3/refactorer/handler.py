"""Step Functions task-handler entry points for the SCP Refactor workflow.

The state machine invokes this Lambda for each task, passing a ``task`` key in
the event to select the operation. This keeps the deployment surface small
(one Lambda function, six tasks) while preserving the Step Functions visual
execution history for observability.

Tasks:
  - load_scp          -> read the SCP by id (fixture or Organizations)
  - check_control_tower -> short-circuit if CT-managed
  - check_size        -> short-circuit if under 8192-byte threshold
  - propose_refactor  -> Kimi K2.5 call via proposer.propose()
  - enumerate_actions -> concrete action list from the catalog
  - verify_equivalence-> IAM Policy Simulator loop
  - create_change_request -> SSM Change Manager start_change_request_execution
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from part3.refactorer import change_request_creator, proposer
from part3.refactorer.equivalence_checker import check as check_equivalence
from shared.control_tower_guard import is_ct_managed
from shared.iam_actions import list_all_actions_for_scp
from shared.logging_config import get_logger, new_correlation_id
from shared.organizations_client import OrganizationsClient

log = get_logger(__name__)

SIZE_THRESHOLD_BYTES = 8192  # 80% of the 10,240 SCP hard limit


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    task = event.get("task")
    if not task:
        raise ValueError("event.task is required")

    correlation_id = event.get("correlationId") or new_correlation_id()
    log.info("refactor_task_start", extra={"task": task, "awsRequestId": getattr(context, "aws_request_id", None)})

    dispatch = {
        "load_scp": _load_scp,
        "check_control_tower": _check_control_tower,
        "check_size": _check_size,
        "propose_refactor": _propose_refactor,
        "enumerate_actions": _enumerate_actions,
        "verify_equivalence": _verify_equivalence,
        "create_change_request": _create_change_request,
    }
    if task not in dispatch:
        raise ValueError(f"Unknown task: {task}")

    result = dispatch[task](event)
    result["correlationId"] = correlation_id
    result["task"] = task
    return result


def _load_scp(event: dict[str, Any]) -> dict[str, Any]:
    scp_id = event.get("scpId")
    if not scp_id:
        raise ValueError("scpId is required for load_scp")
    org = OrganizationsClient()
    policy = org.get_policy(scp_id)
    doc_bytes = len(json.dumps(policy.document))
    return {
        "scpId": policy.policy_id,
        "scpName": policy.name,
        "controlTowerManaged": policy.control_tower_managed,
        "document": policy.document,
        "originalSize": doc_bytes,
    }


def _check_control_tower(event: dict[str, Any]) -> dict[str, Any]:
    ct = event.get("controlTowerManaged") or is_ct_managed(event.get("scpName", ""), event.get("scpId", ""))
    return {"controlTowerManaged": bool(ct), "shortCircuit": bool(ct)}


def _check_size(event: dict[str, Any]) -> dict[str, Any]:
    size = event.get("originalSize") or 0
    over_threshold = size >= SIZE_THRESHOLD_BYTES
    return {"originalSize": size, "overThreshold": over_threshold, "shortCircuit": not over_threshold}


def _propose_refactor(event: dict[str, Any]) -> dict[str, Any]:
    scp_id = event["scpId"]
    scp_name = event.get("scpName", "")
    document = event["document"]
    proposal = proposer.propose(scp_name=scp_name, scp_id=scp_id, document=document)
    return {"proposal": proposal}


def _enumerate_actions(event: dict[str, Any]) -> dict[str, Any]:
    document = event["document"]
    actions = list_all_actions_for_scp(document)
    return {"actionCount": len(actions), "actions": actions}


def _verify_equivalence(event: dict[str, Any]) -> dict[str, Any]:
    original = event["document"]
    proposal = event["proposal"]
    proposed_document = proposal.get("compressed_document") or {}
    scp_id = event["scpId"]
    report = check_equivalence(original=original, proposed=proposed_document, scp_id=scp_id)
    return {
        "equivalence": {
            "isEquivalent": report.is_equivalent,
            "totalActionsChecked": report.total_actions_checked,
            "divergences": [asdict(d) for d in report.divergences],
        }
    }


def _create_change_request(event: dict[str, Any]) -> dict[str, Any]:
    proposal = event["proposal"]
    equivalence = event["equivalence"]
    cr = change_request_creator.create(
        scp_id=event["scpId"],
        scp_name=event.get("scpName", ""),
        original_size=event.get("originalSize") or 0,
        proposed_document=proposal.get("compressed_document") or {},
        equivalence_summary=equivalence,
    )
    return {"changeRequest": cr}
