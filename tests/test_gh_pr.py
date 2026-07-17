import pytest

from bmsdna.devtools.gh_pr import check_bucket, check_label, merge_conflict_message

# Real statusCheckRollup entries captured from `gh pr view 13902 -R cli/cli --json statusCheckRollup`.
COMPLETED_SUCCESS_CHECK_RUN = {
    "__typename": "CheckRun",
    "name": "label-external / label_issues",
    "status": "COMPLETED",
    "conclusion": "SUCCESS",
    "workflowName": "PR Triaging",
}
COMPLETED_SKIPPED_CHECK_RUN = {
    "__typename": "CheckRun",
    "name": "close-from-default-branch / close-from-default-branch",
    "status": "COMPLETED",
    "conclusion": "SKIPPED",
    "workflowName": "PR Triaging",
}


@pytest.mark.parametrize(
    "check,expected_bucket",
    [
        (COMPLETED_SUCCESS_CHECK_RUN, "pass"),
        (COMPLETED_SKIPPED_CHECK_RUN, "skipping"),
        ({"__typename": "CheckRun", "status": "IN_PROGRESS"}, "pending"),
        ({"__typename": "CheckRun", "status": "QUEUED"}, "pending"),
        ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}, "fail"),
        ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "TIMED_OUT"}, "fail"),
        ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"}, "cancel"),
        ({"__typename": "StatusContext", "state": "SUCCESS"}, "pass"),
        ({"__typename": "StatusContext", "state": "PENDING"}, "pending"),
        ({"__typename": "StatusContext", "state": "ERROR"}, "fail"),
        ({"__typename": "StatusContext", "state": "FAILURE"}, "fail"),
    ],
)
def test_check_bucket(check: dict, expected_bucket: str) -> None:
    assert check_bucket(check) == expected_bucket


def test_check_label_prefixes_workflow_when_distinct() -> None:
    assert check_label(COMPLETED_SUCCESS_CHECK_RUN) == "PR Triaging / label-external / label_issues"


def test_check_label_no_duplicate_when_workflow_name_already_in_name() -> None:
    check = {"name": "build (ubuntu-latest)", "workflowName": "build (ubuntu-latest)"}
    assert check_label(check) == "build (ubuntu-latest)"


@pytest.mark.parametrize("mergeable", ["MERGEABLE", "UNKNOWN", None])
def test_merge_conflict_message_none_when_not_conflicting(mergeable: str | None) -> None:
    pr = {"number": 1, "title": "x", "baseRefName": "main", "mergeable": mergeable}
    assert merge_conflict_message(pr) is None


def test_merge_conflict_message_conflicting() -> None:
    pr = {"number": 42, "title": "feat: widgets", "baseRefName": "main", "mergeable": "CONFLICTING"}
    msg = merge_conflict_message(pr)
    assert msg is not None
    assert "PR #42" in msg
    assert "main" in msg
