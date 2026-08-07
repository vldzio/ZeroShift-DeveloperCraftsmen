from shared.cloudtrail_lake_client import count_activity


def test_fixture_mode_returns_seeded_counts():
    counts = count_activity(["s3:GetObject", "s3:DeleteBucket", "iam:AttachRolePolicy"])
    assert counts["s3:GetObject"] > 0
    assert counts["s3:DeleteBucket"] == 0
    assert counts["iam:AttachRolePolicy"] == 0


def test_unknown_actions_default_to_zero():
    counts = count_activity(["fake:NoSuchAction"])
    assert counts["fake:NoSuchAction"] == 0


def test_empty_input_returns_empty_dict():
    assert count_activity([]) == {}
