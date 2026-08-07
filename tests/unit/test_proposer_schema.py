"""Verify the proposer's Bedrock response schema shape and prompt construction."""
from unittest.mock import patch

from part3.refactorer import proposer


def _fake_llm_response():
    from shared.llm_client import LlmResponse

    return LlmResponse(
        output={
            "compressed_document": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Deny",
                        "Action": [
                            "s3:DeleteBucket",
                            "s3:DeleteObject",
                            "s3:PutBucketPolicy",
                        ],
                        "Resource": ["arn:aws:s3:::prod-*"],
                        "Condition": {
                            "StringEquals": {"aws:PrincipalTag/Team": "developers"}
                        },
                    }
                ],
            },
            "statements_removed_or_merged": 12,
            "new_size_bytes": 320,
            "must_split": False,
            "compression_notes": "Merged S3 destructive actions with matching condition.",
        },
        raw={"stubbed": True},
        model_id="moonshotai.kimi-k2.5",
        provider="bedrock",
    )


def test_propose_returns_expected_schema_keys():
    with patch("part3.refactorer.proposer.invoke_structured", return_value=_fake_llm_response()):
        result = proposer.propose(
            scp_name="TestScp",
            scp_id="p-test",
            document={"Version": "2012-10-17", "Statement": []},
        )
    assert "compressed_document" in result
    assert "statements_removed_or_merged" in result
    assert "new_size_bytes" in result
    assert "must_split" in result
    assert "compression_notes" in result
    assert result["must_split"] is False


def test_propose_passes_scp_metadata_into_prompt():
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return _fake_llm_response()

    with patch("part3.refactorer.proposer.invoke_structured", side_effect=_capture):
        proposer.propose(
            scp_name="ProdOversized",
            scp_id="p-prod-oversized-deny",
            document={"Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Action": "s3:*", "Resource": "*"}]},
        )

    assert "ProdOversized" in captured["prompt"]
    assert "p-prod-oversized-deny" in captured["prompt"]
    assert captured["schema_name"] == "scp_refactor_proposal"
    assert captured["temperature"] == 0.1
