import json
from pathlib import Path

import pytest

from part3.denial_analyzer.policy_simulator import DenialContext, LocalScpEvaluator

_FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "scp_policies"


def _load(policy_id: str) -> dict:
    return json.loads((_FIXTURES / f"{policy_id}.json").read_text(encoding="utf-8"))


def test_local_evaluator_detects_region_deny():
    scp = _load("p-prod-deny-regions")
    ctx = DenialContext(
        action="s3:PutObject",
        resource_arn="arn:aws:s3:::prod-app-1-artifacts-mumbai/reports/x.json",
        principal_arn="arn:aws:sts::111111111111:assumed-role/DeveloperRole/session",
        account_id="111111111111",
        region="ap-south-1",
        condition_keys={
            "aws:RequestedRegion": "ap-south-1",
            "aws:PrincipalArn": "arn:aws:sts::111111111111:assumed-role/DeveloperRole/session",
        },
    )
    decision = LocalScpEvaluator().evaluate(
        scp["policyId"], scp["name"], scp["document"], ctx
    )
    assert decision is not None
    assert decision.effect == "Deny"
    assert decision.statement_id == "DenyAllOutsideApprovedRegions"


def test_local_evaluator_allows_approved_region():
    scp = _load("p-prod-deny-regions")
    ctx = DenialContext(
        action="s3:PutObject",
        resource_arn="arn:aws:s3:::bucket/key",
        principal_arn="arn:aws:sts::111111111111:assumed-role/DeveloperRole/session",
        account_id="111111111111",
        region="us-east-1",
        condition_keys={
            "aws:RequestedRegion": "us-east-1",
            "aws:PrincipalArn": "arn:aws:sts::111111111111:assumed-role/DeveloperRole/session",
        },
    )
    decision = LocalScpEvaluator().evaluate(
        scp["policyId"], scp["name"], scp["document"], ctx
    )
    assert decision is None


def test_local_evaluator_detects_instance_type_deny():
    scp = _load("p-root-deny-expensive")
    ctx = DenialContext(
        action="ec2:RunInstances",
        resource_arn="arn:aws:ec2:us-east-1:111111111111:instance/*",
        principal_arn="arn:aws:sts::111111111111:assumed-role/MLEngineerRole/session",
        account_id="111111111111",
        region="us-east-1",
        condition_keys={
            "aws:RequestedRegion": "us-east-1",
            "ec2:InstanceType": "p5.48xlarge",
        },
    )
    decision = LocalScpEvaluator().evaluate(
        scp["policyId"], scp["name"], scp["document"], ctx
    )
    assert decision is not None
    assert decision.statement_id == "DenyLargeInstanceTypes"


def test_local_evaluator_allows_small_instance_type():
    scp = _load("p-root-deny-expensive")
    ctx = DenialContext(
        action="ec2:RunInstances",
        resource_arn="arn:aws:ec2:us-east-1:111111111111:instance/*",
        principal_arn="arn:aws:sts::111111111111:assumed-role/MLEngineerRole/session",
        account_id="111111111111",
        region="us-east-1",
        condition_keys={
            "aws:RequestedRegion": "us-east-1",
            "ec2:InstanceType": "t3.medium",
        },
    )
    assert LocalScpEvaluator().evaluate(
        scp["policyId"], scp["name"], scp["document"], ctx
    ) is None


def test_local_evaluator_detects_iam_wildcard_deny():
    scp = _load("p-account-deny-iam-wildcards")
    ctx = DenialContext(
        action="iam:AttachRolePolicy",
        resource_arn="arn:aws:iam::111111111111:role/AppServiceRole",
        principal_arn="arn:aws:sts::111111111111:assumed-role/PlatformAdminRole/session",
        account_id="111111111111",
        region="us-east-1",
        condition_keys={
            "aws:RequestedRegion": "us-east-1",
            "iam:PolicyARN": "arn:aws:iam::aws:policy/AdministratorAccess",
        },
    )
    decision = LocalScpEvaluator().evaluate(
        scp["policyId"], scp["name"], scp["document"], ctx
    )
    assert decision is not None
    assert decision.statement_id == "DenyIAMWildcardAttachments"


def test_local_evaluator_allows_non_admin_policy():
    scp = _load("p-account-deny-iam-wildcards")
    ctx = DenialContext(
        action="iam:AttachRolePolicy",
        resource_arn="arn:aws:iam::111111111111:role/AppServiceRole",
        principal_arn="arn:aws:sts::111111111111:assumed-role/PlatformAdminRole/session",
        account_id="111111111111",
        region="us-east-1",
        condition_keys={
            "aws:RequestedRegion": "us-east-1",
            "iam:PolicyARN": "arn:aws:iam::aws:policy/ReadOnlyAccess",
        },
    )
    assert LocalScpEvaluator().evaluate(
        scp["policyId"], scp["name"], scp["document"], ctx
    ) is None
