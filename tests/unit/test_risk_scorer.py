from part2.drift_detector.risk_scorer import score


def test_non_prod_unused_only_is_low():
    assert score(environment="non-prod", removed_actions=["s3:DeleteBucket", "s3:PutBucketAcl"]) == "LOW"


def test_prod_is_high():
    assert score(environment="prod", removed_actions=["s3:DeleteBucket"]) == "HIGH"


def test_missing_environment_is_high():
    assert score(environment=None, removed_actions=["s3:DeleteBucket"]) == "HIGH"
    assert score(environment="", removed_actions=["s3:DeleteBucket"]) == "HIGH"


def test_unknown_environment_is_high():
    assert score(environment="staging", removed_actions=["s3:DeleteBucket"]) == "HIGH"


def test_prod_adjacent_is_medium():
    assert score(environment="prod-adjacent", removed_actions=["s3:DeleteBucket"]) == "MEDIUM"


def test_admin_adjacent_removal_bumps_to_medium():
    assert score(environment="non-prod", removed_actions=["iam:CreatePolicy"]) == "MEDIUM"
    assert score(environment="non-prod", removed_actions=["sts:AssumeRole"]) == "MEDIUM"


def test_sensitive_action_removal_bumps_to_high():
    assert score(environment="non-prod", removed_actions=["iam:CreateAccessKey"]) == "HIGH"
    assert score(environment="non-prod", removed_actions=["iam:AttachRolePolicy"]) == "HIGH"


def test_wildcard_scope_reduction_is_medium():
    assert score(
        environment="non-prod",
        removed_actions=["s3:DeleteBucket"],
        is_wildcard_scope_reduction=True,
    ) == "MEDIUM"
