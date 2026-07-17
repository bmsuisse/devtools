import pytest

from bmsdna.devtools.pr_build import merge_conflict_message


@pytest.mark.parametrize("merge_status", ["notSet", "queued", "succeeded"])
def test_merge_conflict_message_none_when_mergeable(merge_status: str) -> None:
    assert merge_conflict_message({"pullRequestId": 1, "title": "x", "mergeStatus": merge_status}) is None


def test_merge_conflict_message_missing_field_is_fine() -> None:
    assert merge_conflict_message({"pullRequestId": 1, "title": "x"}) is None


def test_merge_conflict_message_conflicts() -> None:
    pr = {"pullRequestId": 42, "title": "feat: widgets", "mergeStatus": "conflicts"}
    msg = merge_conflict_message(pr)
    assert msg is not None
    assert "PR #42" in msg
    assert "conflicts" in msg


def test_merge_conflict_message_uses_failure_message_when_present() -> None:
    pr = {
        "pullRequestId": 7,
        "title": "fix: x",
        "mergeStatus": "failure",
        "mergeFailureMessage": "object too large to merge",
    }
    msg = merge_conflict_message(pr)
    assert msg is not None
    assert "object too large to merge" in msg


@pytest.mark.parametrize("merge_status", ["conflicts", "failure", "rejectedByPolicy"])
def test_merge_conflict_message_covers_all_bad_statuses(merge_status: str) -> None:
    pr = {"pullRequestId": 1, "title": "x", "mergeStatus": merge_status}
    assert merge_conflict_message(pr) is not None
