from part2.drift_detector.role_analyzer import analyze
from shared.iam_role_client import ManagedRole


def test_flags_zero_activity_unused_permissions():
    role = ManagedRole(
        role_arn="arn:aws:iam::000000000000:role/test-role",
        role_name="test-role",
        environment="non-prod",
        attached_policy_id="test",
        attached_policy_document={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:DeleteBucket", "s3:GetObject"],
                    "Resource": "*",
                }
            ],
        },
    )
    analysis = analyze(role, lookback_days=90)
    # s3:DeleteBucket has 0 count in the fixture, s3:GetObject has plenty.
    assert "s3:DeleteBucket" in analysis.actions_to_remove
    assert "s3:GetObject" not in analysis.actions_to_remove


def test_deny_statements_are_ignored():
    role = ManagedRole(
        role_arn="arn:aws:iam::000000000000:role/test-role",
        role_name="test-role",
        environment="non-prod",
        attached_policy_id="test",
        attached_policy_document={
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Deny", "Action": "s3:DeleteBucket", "Resource": "*"},
            ],
        },
    )
    analysis = analyze(role, lookback_days=90)
    assert analysis.actions_to_remove == []


def test_empty_policy_produces_no_findings():
    role = ManagedRole(
        role_arn="arn:aws:iam::000000000000:role/empty",
        role_name="empty",
        environment="non-prod",
        attached_policy_id="empty",
        attached_policy_document={"Version": "2012-10-17", "Statement": []},
    )
    analysis = analyze(role, lookback_days=90)
    assert analysis.actions_to_remove == []
    assert analysis.findings == []
