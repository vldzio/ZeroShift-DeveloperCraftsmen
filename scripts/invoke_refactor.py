"""Start a Step Functions execution against the deployed SCP refactor state
machine and poll until it finishes. Prints the execution's final output JSON.

Usage:
    python scripts/invoke_refactor.py <scp-policy-id>
"""
from __future__ import annotations

import json
import sys
import time

import boto3

STATE_MACHINE_NAME = "zeroshift-scp-refactor"


def main(scp_id: str) -> int:
    sfn = boto3.client("stepfunctions")
    sts = boto3.client("sts")

    account_id = sts.get_caller_identity()["Account"]
    region = boto3.session.Session().region_name or "us-east-1"
    sm_arn = f"arn:aws:states:{region}:{account_id}:stateMachine:{STATE_MACHINE_NAME}"

    print(f"Starting execution on {sm_arn} with scpId={scp_id}")
    resp = sfn.start_execution(
        stateMachineArn=sm_arn,
        input=json.dumps({"scpId": scp_id}),
    )
    exec_arn = resp["executionArn"]
    print(f"Execution ARN: {exec_arn}")
    print("Polling for completion (up to 5 minutes)...")

    deadline = time.time() + 300
    last_status = None
    while time.time() < deadline:
        desc = sfn.describe_execution(executionArn=exec_arn)
        status = desc["status"]
        if status != last_status:
            print(f"  status: {status}")
            last_status = status
        if status not in ("RUNNING",):
            output = desc.get("output")
            if output:
                print("Final output:")
                print(json.dumps(json.loads(output), indent=2))
            else:
                print("Execution finished with no output.")
                print(f"  cause: {desc.get('cause')}")
                print(f"  error: {desc.get('error')}")
            return 0 if status == "SUCCEEDED" else 1
        time.sleep(2)

    print("Timed out waiting for execution to finish.")
    return 2


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1]:
        print("Usage: python scripts/invoke_refactor.py <scp-policy-id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
