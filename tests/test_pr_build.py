import pytest

from bmsdna.devtools.pr_build import merge_conflict_message, policy_configs_include_branch

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
