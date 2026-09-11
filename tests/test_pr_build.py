import pytest

from bmsdna.devtools.gitrepo import AdoRemote
from bmsdna.devtools.pr_build import (
    draft_needs_publish_message,
    has_code_review,
    merge_conflict_message,
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


# Shapes captured from a real `.../pullRequests/{id}/threads` response, in order: the PR
# being published, a reviewer joining, casting an "approve" vote, the PR being marked back
# to draft, and Azure DevOps resetting that vote as a result (current reviewers[].vote is
# back to 0 by this point — the "voted 10" thread is the only place that ever happened).
VOTE_UPDATE_THREAD = {"properties": {"CodeReviewThreadType": {"$value": "VoteUpdate"}, "CodeReviewVoteResult": {"$value": "10"}}}
VOTE_RESET_THREAD = {"properties": {"CodeReviewThreadType": {"$value": "VoteReset"}}}
JOINED_REVIEWER_THREAD = {"properties": {"CodeReviewThreadType": {"$value": "ReviewersUpdate"}}}
PLAIN_COMMENT_THREAD = {"comments": [{"commentType": "text", "content": "looks fine so far"}]}


def test_has_code_review_false_when_no_threads() -> None:
    assert has_code_review([]) is False


def test_has_code_review_false_for_non_vote_threads() -> None:
    assert has_code_review([JOINED_REVIEWER_THREAD, PLAIN_COMMENT_THREAD, VOTE_RESET_THREAD]) is False


@pytest.mark.parametrize("vote_result", ["10", "5", "-5", "-10"])
def test_has_code_review_true_for_nonzero_vote_update(vote_result: str) -> None:
    thread = {"properties": {"CodeReviewThreadType": {"$value": "VoteUpdate"}, "CodeReviewVoteResult": {"$value": vote_result}}}
    assert has_code_review([thread]) is True


def test_has_code_review_false_for_zero_vote_update() -> None:
    thread = {"properties": {"CodeReviewThreadType": {"$value": "VoteUpdate"}, "CodeReviewVoteResult": {"$value": "0"}}}
    assert has_code_review([thread]) is False


def test_has_code_review_true_even_after_vote_reset() -> None:
    """The vote survives in its own thread even once a later re-draft resets it."""
    assert has_code_review([VOTE_UPDATE_THREAD, VOTE_RESET_THREAD]) is True


def test_draft_needs_publish_message_none_when_not_draft() -> None:
    pr = {"pullRequestId": 1, "title": "x", "isDraft": False}
    assert draft_needs_publish_message(pr, [VOTE_UPDATE_THREAD]) is None


def test_draft_needs_publish_message_none_when_draft_without_review() -> None:
    pr = {"pullRequestId": 1, "title": "x", "isDraft": True}
    assert draft_needs_publish_message(pr, [JOINED_REVIEWER_THREAD]) is None


def test_draft_needs_publish_message_when_draft_with_review() -> None:
    pr = {"pullRequestId": 42, "title": "feat: widgets", "isDraft": True}
    msg = draft_needs_publish_message(pr, [VOTE_UPDATE_THREAD, VOTE_RESET_THREAD])
    assert msg is not None
    assert "PR #42" in msg
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
