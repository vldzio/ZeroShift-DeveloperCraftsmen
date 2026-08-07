from shared.control_tower_guard import is_ct_managed


def test_aws_guardrails_prefix_detected():
    assert is_ct_managed("aws-guardrails-DenyRootUser") is True


def test_aws_control_tower_prefix_detected():
    assert is_ct_managed("AWSControlTowerBaselineSecurityAudit") is True


def test_regular_scp_not_flagged():
    assert is_ct_managed("DenyNonApprovedRegions") is False
    assert is_ct_managed("DenyExpensiveInstances") is False
