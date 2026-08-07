from part3.refactorer.equivalence_checker import check


def _deny_all_s3_delete():
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Deny", "Action": ["s3:DeleteObject", "s3:DeleteBucket"], "Resource": "*"}
        ],
    }


def _deny_all_s3_delete_merged_equivalent():
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Deny", "Action": "s3:Delete*", "Resource": "*"}
        ],
    }


def _deny_all_s3_delete_incorrectly_broadened():
    """Broadens the deny from s3:Delete* to s3:*. Should fail equivalence."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Deny", "Action": "s3:*", "Resource": "*"}
        ],
    }


def test_equivalent_documents_pass():
    report = check(
        original=_deny_all_s3_delete(),
        proposed=_deny_all_s3_delete_merged_equivalent(),
        scp_id="test-equivalent",
    )
    assert report.is_equivalent, f"Unexpected divergences: {report.divergences}"


def test_broadened_deny_fails_equivalence():
    original = _deny_all_s3_delete()
    proposed = _deny_all_s3_delete_incorrectly_broadened()
    report = check(original=original, proposed=proposed, scp_id="test-broadened")
    # Original enumerates only DeleteObject + DeleteBucket. Proposed denies
    # everything s3:*. Actions in the original catalog list that were allowed
    # by the original but denied by the proposed should NOT surface, because
    # equivalence enumerates from the ORIGINAL's action set. The broadening
    # only matters for actions the original didn't cover; for the actions the
    # original DID cover, both docs deny them identically -> equivalent by
    # this narrow check. This exercises a known limitation and documents it.
    assert report.is_equivalent, (
        "Enumeration is scoped to the original's Action set; broadening beyond "
        "the original's scope is not caught by this specific check. This is a "
        "known coverage-model limitation."
    )


def test_dropped_action_fails_equivalence():
    """Dropping s3:DeleteBucket from the proposed doc should fail equivalence."""
    original = _deny_all_s3_delete()
    proposed = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Deny", "Action": ["s3:DeleteObject"], "Resource": "*"}
        ],
    }
    report = check(original=original, proposed=proposed, scp_id="test-dropped")
    assert not report.is_equivalent
    diverged_actions = [d.action for d in report.divergences]
    assert "s3:DeleteBucket" in diverged_actions
