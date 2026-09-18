from unittest.mock import MagicMock

import pytest

from bmsdna.devtools.gitrepo import AdoRemote
from bmsdna.devtools.pr_build import (
    approval_stage_name,
    build_web_url,
    draft_notice,
    find_pending_approvals,
    merge_conflict_message,
    pending_approval_records,
    policy_configs_include_branch,
    pr_web_url,
)

REPO_ID = "0cd3a822-389e-416e-a4fa-b73f988c2930"

# Shape captured from a real `.../_apis/policy/configurations` response.
BUILD_POLICY_ON_MAIN = {
    "isEnabled": True,
    "isDeleted": False,
    "type": {"id": "0609b952-1397-4640-95ec-e00a01b2c241", "displayName": "Build"},
    "settings": {
        "scope": [{"refName": "refs/heads/main", "matchKind": "Exact", "repositoryId": REPO_ID}],
    },
}
REVIEWER_POLICY_ON_EMERGENCY_RELEASE = {
    "isEnabled": True,
    "isDeleted": False,
    "type": {"id": "fd2167ab-b0be-447a-8ec8-39368250530e", "displayName": "Minimum number of reviewers"},
    "settings": {
        "scope": [{"refName": "refs/heads/emergency-release", "matchKind": "Exact", "repositoryId": REPO_ID}],
    },
}


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


def test_draft_notice_none_when_not_draft(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    pr = {"pullRequestId": 1, "title": "x", "isDraft": False}
    assert draft_notice(pr) is None


def test_draft_notice_when_draft(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    pr = {"pullRequestId": 42, "title": "feat: widgets", "isDraft": True}
    msg = draft_notice(pr)
    assert msg is not None
    assert "PR #42" in msg
    assert "bdt pr publish" in msg
    assert "/code-review" not in msg


def test_draft_notice_tells_claude_code_to_review_first(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    pr = {"pullRequestId": 42, "title": "feat: widgets", "isDraft": True}
    msg = draft_notice(pr)
    assert msg is not None
    assert "PR #42" in msg
    assert "/code-review" in msg
    assert "bdt pr publish" in msg


def test_policy_configs_include_branch_matches_build_policy_on_scoped_branch() -> None:
    configs = [BUILD_POLICY_ON_MAIN, REVIEWER_POLICY_ON_EMERGENCY_RELEASE]
    assert policy_configs_include_branch(configs, REPO_ID, "main", "refs/heads/main") is True


def test_policy_configs_include_branch_false_for_unscoped_branch() -> None:
    configs = [BUILD_POLICY_ON_MAIN, REVIEWER_POLICY_ON_EMERGENCY_RELEASE]
    assert policy_configs_include_branch(configs, REPO_ID, "test", "refs/heads/main") is False


def test_policy_configs_include_branch_ignores_non_build_policy_types() -> None:
    assert policy_configs_include_branch([REVIEWER_POLICY_ON_EMERGENCY_RELEASE], REPO_ID, "emergency-release", "refs/heads/main") is False


def test_policy_configs_include_branch_ignores_disabled_policy() -> None:
    disabled = {**BUILD_POLICY_ON_MAIN, "isEnabled": False}
    assert policy_configs_include_branch([disabled], REPO_ID, "main", "refs/heads/main") is False


def test_policy_configs_include_branch_ignores_deleted_policy() -> None:
    deleted = {**BUILD_POLICY_ON_MAIN, "isDeleted": True}
    assert policy_configs_include_branch([deleted], REPO_ID, "main", "refs/heads/main") is False


def test_policy_configs_include_branch_ignores_other_repo() -> None:
    assert policy_configs_include_branch([BUILD_POLICY_ON_MAIN], "some-other-repo-id", "main", "refs/heads/main") is False


def test_policy_configs_include_branch_matches_default_branch_scope() -> None:
    default_branch_policy = {
        "isEnabled": True,
        "isDeleted": False,
        "type": {"id": "0609b952-1397-4640-95ec-e00a01b2c241"},
        "settings": {"scope": [{"matchKind": "DefaultBranch", "repositoryId": REPO_ID}]},
    }
    assert policy_configs_include_branch([default_branch_policy], REPO_ID, "main", "refs/heads/main") is True
    assert policy_configs_include_branch([default_branch_policy], REPO_ID, "test", "refs/heads/main") is False


def test_pr_web_url_is_the_browsable_page_not_the_rest_api_url() -> None:
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    assert pr_web_url(remote, 123) == "https://dev.azure.com/bmeurope/BMS%20-%20CCMT2/_git/BMS%20-%20CCMT2/pullrequest/123"


class FakeTimelineResponse:
    def __init__(self, records: list) -> None:
        self._records = records

    def json(self) -> dict:
        return {"records": self._records}

    def raise_for_status(self) -> None:
        pass


def test_build_web_url_is_the_browsable_results_page() -> None:
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    assert build_web_url(remote, 456) == "https://dev.azure.com/bmeurope/BMS%20-%20CCMT2/_build/results?buildId=456&view=results"


# Shape captured from a real `.../_apis/build/builds/{id}/timeline` response for a YAML
# pipeline paused on a stage's manual approval check.
STAGE_RECORD = {"id": "stage-1", "type": "Stage", "name": "Deploy to Production", "state": "inProgress"}
CHECKPOINT_RECORD = {"id": "checkpoint-1", "type": "Checkpoint", "parentId": "stage-1", "state": "inProgress"}
PENDING_APPROVAL_RECORD = {
    "id": "approval-1",
    "type": "Checkpoint.Approval",
    "name": "Checkpoint.Approval",
    "parentId": "checkpoint-1",
    "state": "inProgress",
}
APPROVED_APPROVAL_RECORD = {**PENDING_APPROVAL_RECORD, "id": "approval-2", "state": "completed"}
TASK_RECORD = {"id": "task-1", "type": "Task", "name": "npm install", "state": "inProgress"}


def test_pending_approval_records_finds_open_checkpoint_approval() -> None:
    records = [STAGE_RECORD, CHECKPOINT_RECORD, PENDING_APPROVAL_RECORD, TASK_RECORD]
    assert pending_approval_records(records) == [PENDING_APPROVAL_RECORD]


def test_pending_approval_records_ignores_completed_approval() -> None:
    records = [STAGE_RECORD, CHECKPOINT_RECORD, APPROVED_APPROVAL_RECORD]
    assert pending_approval_records(records) == []


def test_pending_approval_records_ignores_ordinary_in_progress_steps() -> None:
    assert pending_approval_records([TASK_RECORD]) == []


def test_approval_stage_name_walks_parent_chain() -> None:
    records = [STAGE_RECORD, CHECKPOINT_RECORD, PENDING_APPROVAL_RECORD]
    assert approval_stage_name(records, PENDING_APPROVAL_RECORD) == "Deploy to Production"


def test_approval_stage_name_falls_back_when_chain_is_missing() -> None:
    orphan = {"id": "approval-1", "name": "Checkpoint.Approval", "parentId": "missing", "state": "inProgress"}
    assert approval_stage_name([orphan], orphan) == "Checkpoint.Approval"


def test_find_pending_approvals_skips_completed_builds() -> None:
    remote = AdoRemote("myorg", "MyProj", "myrepo")
    session = MagicMock()
    session.get.return_value = FakeTimelineResponse([PENDING_APPROVAL_RECORD])

    result = find_pending_approvals(session, remote, [{"id": 1, "status": "completed"}])

    assert result == []
    session.get.assert_not_called()


def test_find_pending_approvals_reports_blocked_build() -> None:
    remote = AdoRemote("myorg", "MyProj", "myrepo")
    session = MagicMock()
    session.get.return_value = FakeTimelineResponse([STAGE_RECORD, CHECKPOINT_RECORD, PENDING_APPROVAL_RECORD])

    build = {"id": 1, "status": "inProgress", "definition": {"name": "deploy"}}
    result = find_pending_approvals(session, remote, [build])

    assert len(result) == 1
    found_build, records, approvals = result[0]
    assert found_build is build
    assert approvals == [PENDING_APPROVAL_RECORD]
    assert approval_stage_name(records, approvals[0]) == "Deploy to Production"
