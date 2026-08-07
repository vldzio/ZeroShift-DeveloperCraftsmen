from shared.iam_actions import (
    all_actions,
    expand_action_pattern,
    expand_service_wildcard,
    list_all_actions_for_scp,
)


def test_service_wildcard_expands_to_concrete_actions():
    actions = expand_service_wildcard("s3")
    assert "s3:GetObject" in actions
    assert "s3:PutObject" in actions
    # No wildcards in the output.
    assert not any("*" in a for a in actions)


def test_expand_action_pattern_handles_prefix_glob():
    actions = expand_action_pattern("s3:Get*")
    assert all(a.startswith("s3:Get") for a in actions)
    assert "s3:GetObject" in actions
    assert "s3:PutObject" not in actions


def test_expand_action_pattern_exact_match():
    assert expand_action_pattern("s3:GetObject") == ["s3:GetObject"]


def test_expand_action_pattern_star_returns_full_catalog():
    assert set(expand_action_pattern("*")) == set(all_actions())


def test_unknown_service_returns_empty_list():
    assert expand_service_wildcard("nonexistent-service-xyz") == []


def test_list_all_actions_for_scp_expands_action():
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Deny", "Action": "s3:*", "Resource": "*"}
        ],
    }
    actions = list_all_actions_for_scp(document)
    assert "s3:GetObject" in actions
    assert "s3:PutObject" in actions


def test_list_all_actions_for_scp_handles_not_action():
    # NotAction: iam:* -> the SCP applies to everything EXCEPT iam actions
    # within the services referenced (only iam here) => empty for iam alone.
    # A more realistic case combines Action and NotAction across services.
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Deny",
                "NotAction": ["iam:*", "sts:*"],
                "Resource": "*",
            }
        ],
    }
    actions = list_all_actions_for_scp(document)
    # NotAction with only iam and sts and no positive Action means the
    # complement of iam+sts within {iam, sts} is empty. Ensure the function
    # still returns something coherent (falls back to full catalog).
    assert len(actions) > 0


def test_list_all_actions_dedupes():
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Deny", "Action": "s3:*", "Resource": "*"},
            {"Effect": "Deny", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "*"},
        ],
    }
    actions = list_all_actions_for_scp(document)
    assert len(actions) == len(set(actions))
