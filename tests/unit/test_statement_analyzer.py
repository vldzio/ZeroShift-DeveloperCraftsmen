from part3.stale_detector.statement_analyzer import (
    analyze_statement,
    enumerate_statement_actions,
)


def test_dead_deny_flagged_when_all_actions_zero():
    stmt = {
        "Sid": "DenyAllUnusedIam",
        "Effect": "Deny",
        "Action": ["iam:AttachRolePolicy", "iam:PutRolePolicy"],
        "Resource": "*",
    }
    counts = {"iam:AttachRolePolicy": 0, "iam:PutRolePolicy": 0}
    finding = analyze_statement(
        scp_id="p-test",
        scp_name="TestSCP",
        statement=stmt,
        action_counts=counts,
        lookback_days=180,
    )
    assert finding.is_stale is True
    assert finding.total_activity == 0
    assert "iam:AttachRolePolicy" in finding.actions
    assert "no cloudtrail activity" in finding.recommendation.lower()


def test_mixed_counts_not_flagged():
    stmt = {
        "Sid": "DenyMixed",
        "Effect": "Deny",
        "Action": ["s3:GetObject", "s3:DeleteBucket"],
        "Resource": "*",
    }
    counts = {"s3:GetObject": 500, "s3:DeleteBucket": 0}
    finding = analyze_statement(
        scp_id="p-test",
        scp_name="TestSCP",
        statement=stmt,
        action_counts=counts,
        lookback_days=180,
    )
    assert finding.is_stale is False
    assert finding.total_activity == 500


def test_allow_statement_never_flagged():
    stmt = {
        "Sid": "AllowSomething",
        "Effect": "Allow",
        "Action": ["s3:GetObject"],
        "Resource": "*",
    }
    counts = {"s3:GetObject": 0}
    finding = analyze_statement(
        scp_id="p-test",
        scp_name="TestSCP",
        statement=stmt,
        action_counts=counts,
        lookback_days=180,
    )
    assert finding.is_stale is False
    assert finding.statement_effect == "Allow"


def test_wildcard_action_expands_before_analyzing():
    stmt = {
        "Sid": "DenyS3Get",
        "Effect": "Deny",
        "Action": "s3:Get*",
        "Resource": "*",
    }
    actions = enumerate_statement_actions(stmt)
    assert all(a.startswith("s3:Get") for a in actions)
    assert len(actions) > 0
