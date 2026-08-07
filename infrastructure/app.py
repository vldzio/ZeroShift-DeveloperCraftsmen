"""CDK app entry point for the ZeroShift platform."""
from __future__ import annotations

import os

import aws_cdk as cdk

from stacks.part1_stack import Part1Stack
from stacks.part2_stack import Part2Stack
from stacks.part3_stack import Part3Stack
from stacks.shared_stack import SharedStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

shared = SharedStack(app, "ZeroShiftShared", env=env)

part3 = Part3Stack(
    app,
    "ZeroShiftPart3",
    env=env,
    intent_table=shared.intent_table,
    scp_audit_table=shared.scp_audit_table,
    denial_alerts_topic=shared.denial_alerts_topic,
    approval_requests_topic=shared.approval_requests_topic,
    llm_credentials_secret=shared.llm_credentials_secret,
    shared_kms_key=shared.shared_kms_key,
    agent_layer=shared.agent_layer,
    agent_reasoning_table=shared.agent_reasoning_table,
)

# Part 1 populates its stack. Depends on Part 3's HttpApi to share routes.
Part1Stack(app, "ZeroShiftPart1", env=env, shared=shared, part3=part3)

# Part 2 populates its stack. Depends on Part 3's HttpApi to share routes.
Part2Stack(app, "ZeroShiftPart2", env=env, shared=shared, part3=part3)

app.synth()
