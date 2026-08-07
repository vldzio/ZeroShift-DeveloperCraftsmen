from unittest.mock import patch

from part2.drift_detector import policy_proposer


def _fake_llm(*args, **kwargs):
    from shared.llm_client import LlmResponse

    return LlmResponse(
        output={
            "proposed_policy": {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "*"},
                ],
            },
            "removed_actions": ["s3:DeleteBucket", "s3:PutBucketAcl"],
            "rationale": "Removed two unused destructive S3 actions with zero CloudTrail activity.",
            "warnings": [],
        },
        raw={"stubbed": True},
        model_id="moonshotai.kimi-k2.5",
        provider="bedrock",
    )


def test_empty_removals_returns_original_unchanged():
    original = {"Version": "2012-10-17", "Statement": []}
    result = policy_proposer.propose(
        role_arn="arn:aws:iam::0:role/test",
        original_policy=original,
        actions_to_remove=[],
    )
    assert result["proposed_policy"] == original
    assert result["removed_actions"] == []


def test_calls_llm_when_removals_present():
    original = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:DeleteBucket", "s3:PutBucketAcl"], "Resource": "*"}
        ],
    }
    with patch("part2.drift_detector.policy_proposer.invoke_structured", side_effect=_fake_llm):
        result = policy_proposer.propose(
            role_arn="arn:aws:iam::0:role/test",
            original_policy=original,
            actions_to_remove=["s3:DeleteBucket", "s3:PutBucketAcl"],
        )
    assert "proposed_policy" in result
    assert "rationale" in result
    assert set(result["removed_actions"]) == {"s3:DeleteBucket", "s3:PutBucketAcl"}
