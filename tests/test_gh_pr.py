from unittest.mock import MagicMock

import pytest

from bmsdna.devtools.cli_tools import CLI_TIMEOUT_SECS, EXIT_NEEDS_APPROVAL
from bmsdna.devtools.gh_pr import (
    CLI_UPLOAD_TIMEOUT_SECS,
    add_attachments,
    add_files,
    check_bucket,
    check_label,
    head_branch,
    comment_with_screenshots,
    create,
    deploy_run_hint,
    draft_notice,
    failed_run_ids,
    get_pr,
    get_pr_body,
    get_workflow_runs_for_branch,
    has_build_policy,
    latest_per_workflow,
    link_issue_to_pr,
    merge_conflict_message,
    print_failed_step_logs,
    protection_requires_status_checks,
    publish,
    push_assets,
    resolve_pr_id,
    retry,
    retry_hint,
    run,
    run_watch_deploy,
    set_draft,
    update,
)

PULLS = "repos/owner/repo/pulls"


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeGH:
    """Stands in for `GitHubClient`: records `(method, path, payload)` and answers from `routes`
    (`(method, path)` -> value, or a callable taking the payload). A route value that is a
    `SystemExit` is raised, like the real client does on an HTTP error."""

    owner = "owner"
    repo = "repo"
    repo_path = "repos/owner/repo"

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, str, object]] = []

    def _answer(self, method: str, path: str, payload: object) -> object:
        self.calls.append((method, path, payload))
        if (method, path) not in self.routes:
            raise AssertionError(f"unexpected request {method} {path} {payload}")
        value = self.routes[(method, path)]
        if callable(value):
            value = value(payload)
        if isinstance(value, SystemExit):
            raise value
        return value

    def get(self, path, params=None, **kwargs):
        return self._answer("GET", path, params)

    def post(self, path, body=None, **kwargs):
        return self._answer("POST", path, body)

    def patch(self, path, body=None, **kwargs):
        return self._answer("PATCH", path, body)

    def graphql(self, query, **variables):
        op = next((n for n in ("markPullRequestReadyForReview", "convertPullRequestToDraft", "enablePullRequestAutoMerge", "query ContextsPage") if n in query), "query")
        return self._answer("GRAPHQL", op, variables)

    def request(self, method, path, *, ok=(), **kwargs):
        return self._answer(method, path, kwargs.get("params"))

    def paginate(self, path, params=None, **kwargs):
        return self._answer("GET", path, params)

    def sent(self, method: str, path: str | None = None) -> list[object]:
        return [p for m, pa, p in self.calls if m == method and (path is None or pa == path)]


@pytest.fixture(autouse=True)
def _on_branch(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.current_branch", lambda: "feature-x")


def rest_pr(number: int = 7, body: str = "existing body", title: str = "Fix bug", head: str = "feature-x", state: str = "open", draft: bool = True) -> dict:
    return {"number": number, "title": title, "body": body, "state": state, "node_id": f"PR_node_{number}", "draft": draft, "head": {"ref": head}, "base": {"ref": "main"}, "html_url": f"https://github.com/owner/repo/pull/{number}"}


def view_routes(number: int = 7, **kwargs) -> dict:
    """Routes for resolving the current branch's PR (#number) and reading it."""
    return {
        ("GET", PULLS): [rest_pr(number, **kwargs)],
        ("GET", f"{PULLS}/{number}"): rest_pr(number, **kwargs),
    }


def pr_graphql_response(**overrides) -> dict:
    pr = {
        "id": "PR_node_7",
        "number": 7,
        "title": "feat: x",
        "body": "b",
        "url": "https://github.com/owner/repo/pull/7",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "baseRefName": "main",
        "headRefName": "feature-x",
        "closingIssuesReferences": {"nodes": [{"number": 3, "url": "u", "repository": {"name": "repo", "owner": {"login": "owner"}}}]},
        "statusCheckRollup": {
            "contexts": {
                "nodes": [
                    {
                        "__typename": "CheckRun",
                        "name": "build",
                        "status": "COMPLETED",
                        "conclusion": "FAILURE",
                        "detailsUrl": "https://github.com/owner/repo/actions/runs/5/job/6",
                        "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}},
                    },
                    {"__typename": "StatusContext", "context": "ext", "state": "PENDING", "targetUrl": "t"},
                ]
            }
        },
    }
    return {"repository": {"pullRequest": {**pr, **overrides}}}


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
FAILED_CHECK_RUN = {
    "__typename": "CheckRun",
    "name": "build (ubuntu-latest)",
    "status": "COMPLETED",
    "conclusion": "FAILURE",
    "workflowName": "Python Test",
    "detailsUrl": "https://github.com/owner/repo/actions/runs/34882319228/job/104104355022",
}
FAILED_CHECK_RUN_SAME_RUN = {
    "__typename": "CheckRun",
    "name": "build (windows-latest)",
    "status": "COMPLETED",
    "conclusion": "FAILURE",
    "workflowName": "Python Test",
    "detailsUrl": "https://github.com/owner/repo/actions/runs/34882319228/job/104104399999",
}
FAILED_STATUS_CONTEXT = {"__typename": "StatusContext", "state": "FAILURE", "context": "external-ci"}


@pytest.mark.parametrize(
    "check,expected_bucket",
    [
        (COMPLETED_SUCCESS_CHECK_RUN, "pass"),
        (COMPLETED_SKIPPED_CHECK_RUN, "skipping"),
        ({"__typename": "CheckRun", "status": "IN_PROGRESS"}, "pending"),
        ({"__typename": "CheckRun", "status": "QUEUED"}, "pending"),
        ({"__typename": "CheckRun", "status": "WAITING"}, "waiting_approval"),
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


def test_run_wait_exits_1_not_2_when_a_check_already_failed_and_another_needs_approval(monkeypatch) -> None:
    """Regression: an already-failed check elsewhere in the PR must still end --wait even when
    another check is separately waiting on a deployment approval — must report the failure
    (exit 1), not silently prioritize the approval prompt (exit 2) or hang forever.
    """
    pr = {
        "number": 42,
        "title": "feat: widgets",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "isDraft": False,
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "deploy", "status": "WAITING", "workflowName": "Deploy"},
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "FAILURE", "workflowName": "CI"},
        ],
    }
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: pr)

    with pytest.raises(SystemExit) as exc_info:
        run("gh", wait=True)

    assert exc_info.value.code == 1


def test_run_wait_does_not_hang_when_a_stuck_pending_check_also_exists(monkeypatch) -> None:
    """Regression: a genuinely-stuck pending check (e.g. downstream of the blocked deployment
    gate, so it can never leave "pending" on its own) combined with an already-failed check
    and a waiting-approval check must not send --wait into an infinite poll loop.
    """
    pr = {
        "number": 42,
        "title": "feat: widgets",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "isDraft": False,
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "deploy", "status": "WAITING", "workflowName": "Deploy"},
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "FAILURE", "workflowName": "CI"},
            {"__typename": "CheckRun", "name": "downstream", "status": "QUEUED", "workflowName": "CI"},
        ],
    }
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: pr)
    monkeypatch.setattr("bmsdna.devtools.gh_pr.time.sleep", lambda s: pytest.fail("must not poll — would hang --wait forever"))

    with pytest.raises(SystemExit) as exc_info:
        run("gh", wait=True)

    assert exc_info.value.code == 1


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


def test_draft_notice_none_when_not_draft(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    pr = {"number": 1, "title": "x", "isDraft": False}
    assert draft_notice(pr) is None


def test_draft_notice_when_draft(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    pr = {"number": 42, "title": "feat: widgets", "isDraft": True}
    msg = draft_notice(pr)
    assert msg is not None
    assert "PR #42" in msg
    assert "bdt pr publish" in msg
    assert "/code-review" not in msg


def test_draft_notice_tells_claude_code_to_review_first(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    pr = {"number": 42, "title": "feat: widgets", "isDraft": True}
    msg = draft_notice(pr)
    assert msg is not None
    assert "PR #42" in msg
    assert "/code-review" in msg
    assert "bdt pr publish" in msg


def test_protection_requires_status_checks_true_when_configured() -> None:
    protection = {"required_status_checks": {"strict": True, "contexts": ["build"]}}
    assert protection_requires_status_checks(protection) is True


@pytest.mark.parametrize("protection", [{}, {"required_status_checks": None}])
def test_protection_requires_status_checks_false_when_absent(protection: dict) -> None:
    assert protection_requires_status_checks(protection) is False




def test_resolve_pr_id_uses_given_id_without_any_request() -> None:
    gh = FakeGH()
    assert resolve_pr_id(gh, 99) == 99  # type: ignore[arg-type]
    assert gh.calls == []


def test_get_pr_with_pr_id_resolves_that_pr_number_directly() -> None:
    """`--pr-id` must query the given PR directly, not whatever the current branch has."""
    gh = FakeGH({("GRAPHQL", "query"): pr_graphql_response()})

    get_pr(gh, pr_id=99)  # type: ignore[arg-type]

    assert gh.calls[0][2] == {"owner": "owner", "repo": "repo", "number": 99}


def test_get_pr_without_pr_id_uses_the_current_branchs_pr() -> None:
    gh = FakeGH({**view_routes(7), ("GRAPHQL", "query"): pr_graphql_response()})

    get_pr(gh)  # type: ignore[arg-type]

    assert gh.sent("GET", PULLS) == [{"head": "owner:feature-x", "state": "all", "sort": "created", "direction": "desc"}]
    assert gh.sent("GRAPHQL", "query") == [{"owner": "owner", "repo": "repo", "number": 7}]


def test_get_pr_prefers_the_open_pr_over_a_newer_closed_one() -> None:
    closed, opened = rest_pr(9, state="closed"), rest_pr(8, state="open")
    gh = FakeGH({("GET", PULLS): [closed, opened], ("GRAPHQL", "query"): pr_graphql_response()})

    get_pr(gh)  # type: ignore[arg-type]

    assert gh.sent("GRAPHQL", "query")[0]["number"] == 8  # type: ignore[index]


def test_get_pr_falls_back_to_a_closed_pr_when_none_is_open() -> None:
    gh = FakeGH({("GET", PULLS): [rest_pr(9, state="closed")], ("GRAPHQL", "query"): pr_graphql_response()})

    get_pr(gh)  # type: ignore[arg-type]

    assert gh.sent("GRAPHQL", "query")[0]["number"] == 9  # type: ignore[index]


def test_get_pr_exits_when_the_branch_has_no_pr() -> None:
    with pytest.raises(SystemExit, match="feature-x"):
        get_pr(FakeGH({("GET", PULLS): []}))  # type: ignore[arg-type]


def test_get_pr_keeps_the_gh_pr_view_shape() -> None:
    gh = FakeGH({("GRAPHQL", "query"): pr_graphql_response()})

    pr = get_pr(gh, 7, fields="number,title,url,state,isDraft,mergeable,baseRefName,closingIssuesReferences,statusCheckRollup")  # type: ignore[arg-type]

    assert pr["number"] == 7
    assert pr["mergeable"] == "MERGEABLE"
    assert pr["closingIssuesReferences"] == [{"number": 3, "url": "u", "repository": {"name": "repo", "owner": {"login": "owner"}}}]
    build, ctx = pr["statusCheckRollup"]
    assert build["workflowName"] == "CI" and "checkSuite" not in build
    assert check_bucket(build) == "fail" and failed_run_ids(pr["statusCheckRollup"]) == [5]
    assert check_bucket(ctx) == "pending"


def test_get_pr_only_returns_the_requested_fields() -> None:
    gh = FakeGH({("GRAPHQL", "query"): pr_graphql_response()})
    assert set(get_pr(gh, 7, fields="number,isDraft")) == {"number", "isDraft"}  # type: ignore[arg-type]


def test_get_pr_without_checks_has_an_empty_rollup() -> None:
    gh = FakeGH({("GRAPHQL", "query"): pr_graphql_response(statusCheckRollup=None, closingIssuesReferences=None)})
    pr = get_pr(gh, 7, fields="statusCheckRollup,closingIssuesReferences")  # type: ignore[arg-type]
    assert pr == {"statusCheckRollup": [], "closingIssuesReferences": []}


def test_get_pr_exits_when_the_pr_does_not_exist() -> None:
    gh = FakeGH({("GRAPHQL", "query"): {"repository": {"pullRequest": None}}})
    with pytest.raises(SystemExit, match="#99"):
        get_pr(gh, 99)  # type: ignore[arg-type]


def test_create_passes_labels_after_creating_the_pr(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: [("feat: x", "why")])
    gh = FakeGH(
        {
            ("POST", PULLS): {"number": 7, "html_url": "https://github.com/owner/repo/pull/7"},
            ("POST", "repos/owner/repo/issues/7/labels"): [],
        }
    )

    returncode, url = create(gh, "main", [], labels=["bug", "urgent"])  # type: ignore[arg-type]

    assert (returncode, url) == (0, "https://github.com/owner/repo/pull/7")
    assert gh.sent("POST", "repos/owner/repo/issues/7/labels") == [{"labels": ["bug", "urgent"]}]


def test_create_fills_title_and_body_from_a_single_commit(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: [("feat: x", "because y")])
    gh = FakeGH({("POST", PULLS): {"number": 7, "html_url": "u"}})

    create(gh, "main", [], draft=True)  # type: ignore[arg-type]

    assert gh.sent("POST", PULLS) == [{"title": "feat: x", "body": "because y", "head": "feature-x", "base": "main", "draft": True}]


def test_create_fills_from_branch_name_and_subjects_for_several_commits(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: [("one", ""), ("two", "")])
    gh = FakeGH({("POST", PULLS): {"number": 7, "html_url": "u"}})

    create(gh, "main", [])  # type: ignore[arg-type]

    payload = gh.sent("POST", PULLS)[0]
    assert payload["title"] == "Feature x"  # type: ignore[index]
    assert payload["body"] == "- one\n- two"  # type: ignore[index]


def test_create_explicit_title_and_description_override_the_fill(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: [("feat: x", "because y")])
    gh = FakeGH({("POST", PULLS): {"number": 7, "html_url": "u"}})

    create(gh, "main", [], title="My title", description="My body")  # type: ignore[arg-type]

    payload = gh.sent("POST", PULLS)[0]
    assert (payload["title"], payload["body"]) == ("My title", "My body")  # type: ignore[index]


def test_create_extra_args_title_and_body_forms_win(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: [])
    gh = FakeGH({("POST", PULLS): {"number": 7, "html_url": "u"}})

    create(gh, "main", ["--title", "T", "--body=B"], title="ignored")  # type: ignore[arg-type]

    payload = gh.sent("POST", PULLS)[0]
    assert (payload["title"], payload["body"]) == ("T", "B")  # type: ignore[index]


@pytest.mark.parametrize("flag", ["--template", "-T", "--recover", "--editor", "--delete-nothing"])
def test_create_rejects_unsupported_extra_args(flag) -> None:
    gh = FakeGH()
    with pytest.raises(SystemExit, match="unsupported (flag|argument).*Supported flags:.*--reviewer"):
        create(gh, "main", [flag, "x"])  # type: ignore[arg-type]
    assert gh.calls == []


def test_create_missing_flag_value_exits() -> None:
    with pytest.raises(SystemExit, match="--base needs a value"):
        create(FakeGH(), "main", ["--base"])  # type: ignore[arg-type]


def _created(monkeypatch, commits=(("feat: x", "because y"),), extra_routes=None):
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: list(commits))
    return FakeGH({("POST", PULLS): {"number": 7, "node_id": "PR_node_7", "html_url": "https://github.com/owner/repo/pull/7"}, **(extra_routes or {})})


def test_create_description_is_only_a_long_alias_of_body(monkeypatch) -> None:
    gh = _created(monkeypatch)
    create(gh, "main", ["--description", "D"])  # type: ignore[arg-type]
    assert gh.sent("POST", PULLS)[0]["body"] == "D"  # type: ignore[index]


def test_create_dash_d_is_draft_not_description(monkeypatch) -> None:
    gh = _created(monkeypatch)
    create(gh, "main", ["-d"])  # type: ignore[arg-type]
    payload = gh.sent("POST", PULLS)[0]
    assert payload["draft"] is True and payload["body"] == "because y"  # type: ignore[index]


def test_create_draft_false_overrides_the_default(monkeypatch) -> None:
    gh = _created(monkeypatch)
    create(gh, "main", ["--draft=false"], draft=True)  # type: ignore[arg-type]
    assert gh.sent("POST", PULLS)[0]["draft"] is False  # type: ignore[index]


def test_create_base_and_head_override_target_and_branch(monkeypatch) -> None:
    gh = _created(monkeypatch)
    create(gh, "main", ["--base", "release", "-H=someone:topic"])  # type: ignore[arg-type]
    payload = gh.sent("POST", PULLS)[0]
    assert (payload["base"], payload["head"]) == ("release", "someone:topic")  # type: ignore[index]


def test_create_body_file_and_conflict(monkeypatch, tmp_path) -> None:
    f = tmp_path / "b.md"
    f.write_text("file body")
    gh = _created(monkeypatch)
    create(gh, "main", ["-F", str(f)])  # type: ignore[arg-type]
    assert gh.sent("POST", PULLS)[0]["body"] == "file body"  # type: ignore[index]
    with pytest.raises(SystemExit, match="either --body/--description or --body-file"):
        create(gh, "main", ["--body", "x", "--body-file", str(f)])  # type: ignore[arg-type]


def test_create_body_file_from_stdin(monkeypatch) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("piped"))
    gh = _created(monkeypatch)
    create(gh, "main", ["--body-file", "-"])  # type: ignore[arg-type]
    assert gh.sent("POST", PULLS)[0]["body"] == "piped"  # type: ignore[index]


def test_create_labels_merge_bdt_and_passthrough(monkeypatch) -> None:
    gh = _created(monkeypatch, extra_routes={("POST", "repos/owner/repo/issues/7/labels"): []})
    create(gh, "main", ["-l", "b,c", "--label=a"], labels=["a", "z"])  # type: ignore[arg-type]
    assert gh.sent("POST", "repos/owner/repo/issues/7/labels") == [{"labels": ["a", "z", "b", "c"]}]


def test_create_assignee_milestone_reviewer_flags(monkeypatch) -> None:
    gh = _created(
        monkeypatch,
        extra_routes={
            ("GET", "user"): {"login": "me"},
            ("GET", "repos/owner/repo/milestones"): [{"title": "v1", "number": 3}],
            ("POST", "repos/owner/repo/issues/7/assignees"): {},
            ("PATCH", "repos/owner/repo/issues/7"): {},
            ("POST", f"{PULLS}/7/requested_reviewers"): {},
        },
    )
    create(gh, "main", ["-a", "jane,@me", "-m", "V1", "-r", "bob,acme/core", "--reviewer=eve"])  # type: ignore[arg-type]
    assert gh.sent("POST", "repos/owner/repo/issues/7/assignees") == [{"assignees": ["jane", "me"]}]
    assert gh.sent("PATCH", "repos/owner/repo/issues/7") == [{"milestone": 3}]
    assert gh.sent("POST", f"{PULLS}/7/requested_reviewers") == [{"reviewers": ["bob", "eve"], "team_reviewers": ["core"]}]


def test_create_unknown_milestone_fails_before_creating(monkeypatch) -> None:
    gh = _created(monkeypatch, extra_routes={("GET", "repos/owner/repo/milestones"): []})
    assert create(gh, "main", ["-m", "nope"]) == (1, None)  # type: ignore[arg-type]
    assert gh.sent("POST", PULLS) == []


def test_create_follow_up_failures_are_warnings(monkeypatch, capsys) -> None:
    gh = _created(
        monkeypatch,
        extra_routes={
            ("POST", "repos/owner/repo/issues/7/assignees"): SystemExit("no"),
            ("POST", f"{PULLS}/7/requested_reviewers"): SystemExit("denied"),
        },
    )
    assert create(gh, "main", ["-a", "jane", "-r", "bob"]) == (0, "https://github.com/owner/repo/pull/7")  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert "adding assignee(s) failed: no" in err and "requesting review failed: denied" in err


def test_create_project_flag_adds_the_pr_to_the_board(monkeypatch) -> None:
    gh = _created(monkeypatch)
    added: list[tuple[str, str]] = []
    monkeypatch.setattr("bmsdna.devtools.gh_issue.find_project", lambda gh, owner, b: {"id": f"P_{b}"})
    monkeypatch.setattr("bmsdna.devtools.gh_issue.add_to_project", lambda gh, project, node: added.append((project["id"], node)))
    create(gh, "main", ["--project", "Roadmap"])  # type: ignore[arg-type]
    assert added == [("P_Roadmap", "PR_node_7")]


def test_create_auto_complete_enables_auto_merge_with_squash(monkeypatch) -> None:
    gh = _created(monkeypatch, extra_routes={("GRAPHQL", "enablePullRequestAutoMerge"): {}})
    create(gh, "main", ["--auto-complete", "--squash", "--merge-commit-message", "msg"])  # type: ignore[arg-type]
    assert gh.sent("GRAPHQL", "enablePullRequestAutoMerge") == [{"pr": "PR_node_7", "method": "SQUASH", "headline": "msg"}]


def test_create_auto_merge_failure_is_a_warning(monkeypatch, capsys) -> None:
    gh = _created(monkeypatch, extra_routes={("GRAPHQL", "enablePullRequestAutoMerge"): SystemExit("not allowed")})
    assert create(gh, "main", ["--auto-complete"])[0] == 0  # type: ignore[arg-type]
    assert "enabling auto-merge" in capsys.readouterr().err


def test_create_work_items_link_the_pr_with_fixes(monkeypatch) -> None:
    gh = _created(monkeypatch)
    linked: list[tuple[int, str, int]] = []
    monkeypatch.setattr("bmsdna.devtools.gh_pr.link_issue_to_pr", lambda gh, n, body, issue: linked.append((n, body, issue)) or body)
    create(gh, "main", ["--work-items", "12,#13"])  # type: ignore[arg-type]
    assert [(n, i) for n, _, i in linked] == [(7, 12), (7, 13)]


def test_create_notes_what_github_cannot_honour(monkeypatch, capsys) -> None:
    gh = _created(monkeypatch)
    create(gh, "main", ["--delete-source-branch", "--bypass-policy", "--squash"])  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert "--delete-source-branch is ignored on GitHub" in err
    assert "--bypass-policy is ignored on GitHub" in err
    assert "--squash is ignored on GitHub without --auto-complete" in err


def test_create_accepts_a_parsed_options_object(monkeypatch) -> None:
    from bmsdna.devtools.pr_create_args import PrCreateOptions

    gh = _created(monkeypatch)
    create(gh, "main", [], options=PrCreateOptions(title="T", base="release", draft=False), draft=True)  # type: ignore[arg-type]
    payload = gh.sent("POST", PULLS)[0]
    assert (payload["title"], payload["base"], payload["draft"]) == ("T", "release", False)  # type: ignore[index]


def test_create_no_maintainer_edit(monkeypatch) -> None:
    gh = _created(monkeypatch)
    create(gh, "main", ["--no-maintainer-edit"])  # type: ignore[arg-type]
    assert gh.sent("POST", PULLS)[0]["maintainer_can_modify"] is False  # type: ignore[index]


def test_create_fill_first_uses_the_first_commit(monkeypatch) -> None:
    gh = _created(monkeypatch, commits=[("first", "b1"), ("second", "b2")])
    create(gh, "main", ["--fill-first"])  # type: ignore[arg-type]
    payload = gh.sent("POST", PULLS)[0]
    assert (payload["title"], payload["body"]) == ("first", "b1")  # type: ignore[index]


def test_create_fill_verbose_body_has_every_commit_message(monkeypatch) -> None:
    gh = _created(monkeypatch, commits=[("first", "b1"), ("second", "")])
    create(gh, "main", ["--fill-verbose"])  # type: ignore[arg-type]
    payload = gh.sent("POST", PULLS)[0]
    assert payload["title"] == "Feature x" and payload["body"] == "first\n\nb1\n\nsecond"  # type: ignore[index]


def test_create_fill_flag_is_accepted(monkeypatch) -> None:
    gh = _created(monkeypatch)
    create(gh, "main", ["--fill"])  # type: ignore[arg-type]
    assert gh.sent("POST", PULLS)[0]["title"] == "feat: x"  # type: ignore[index]


def test_create_dry_run_creates_nothing(monkeypatch, capsys) -> None:
    gh = _created(monkeypatch)
    assert create(gh, "main", ["--dry-run", "-l", "bug", "-a", "jane"]) == (0, None)  # type: ignore[arg-type]
    assert gh.calls == []
    out = capsys.readouterr().out
    assert "title: feat: x" in out and "labels: ['bug']" in out and "assignees: ['jane']" in out


def test_create_web_opens_the_pr(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.gh_args.webbrowser.open", lambda url: opened.append(url))
    gh = _created(monkeypatch)
    create(gh, "main", ["--web"])  # type: ignore[arg-type]
    assert opened == ["https://github.com/owner/repo/pull/7"]


def test_create_returns_none_url_on_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: [])
    gh = FakeGH({("POST", PULLS): SystemExit("GitHub POST failed (HTTP 422): head invalid")})

    returncode, url = create(gh, "main", [])  # type: ignore[arg-type]

    assert returncode == 1
    assert url is None
    assert "head invalid" in capsys.readouterr().err


def test_create_still_succeeds_when_a_label_cannot_be_added(monkeypatch, capsys) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: [])
    gh = FakeGH(
        {("POST", PULLS): {"number": 7, "html_url": "u"}, ("POST", "repos/owner/repo/issues/7/labels"): SystemExit("forbidden")}
    )

    assert create(gh, "main", [], labels=["x"]) == (0, "u")  # type: ignore[arg-type]
    assert "forbidden" in capsys.readouterr().err


def test_publish_marks_the_current_branchs_pr_ready() -> None:
    gh = FakeGH({**view_routes(7), ("GRAPHQL", "markPullRequestReadyForReview"): {}})

    publish(gh)  # type: ignore[arg-type]

    assert gh.sent("GRAPHQL", "markPullRequestReadyForReview") == [{"id": "PR_node_7"}]


def test_publish_leaves_an_already_ready_pr_alone(capsys) -> None:
    gh = FakeGH({("GET", f"{PULLS}/99"): rest_pr(99, draft=False)})

    publish(gh, pr_id=99)  # type: ignore[arg-type]

    assert gh.sent("GRAPHQL", "markPullRequestReadyForReview") == []
    assert "already ready for review" in capsys.readouterr().out


def test_publish_with_pr_id_targets_that_pr_number() -> None:
    gh = FakeGH({("GET", f"{PULLS}/99"): rest_pr(99), ("GRAPHQL", "markPullRequestReadyForReview"): {}})

    publish(gh, pr_id=99)  # type: ignore[arg-type]

    assert gh.sent("GRAPHQL", "markPullRequestReadyForReview") == [{"id": "PR_node_99"}]
    assert gh.sent("GET", PULLS) == []


def test_publish_exits_with_the_api_error_message() -> None:
    gh = FakeGH({("GET", f"{PULLS}/99"): SystemExit("Not Found")})

    with pytest.raises(SystemExit) as exc_info:
        publish(gh, pr_id=99)  # type: ignore[arg-type]

    assert "Not Found" in str(exc_info.value)


def test_set_draft_with_pr_id_converts_that_pr_number() -> None:
    gh = FakeGH(
        {
            ("GRAPHQL", "query"): pr_graphql_response(number=99, id="PR_node_99"),
            ("GRAPHQL", "convertPullRequestToDraft"): {},
        }
    )

    assert set_draft(gh, pr_id=99) is True  # type: ignore[arg-type]
    assert gh.sent("GRAPHQL", "query")[0]["number"] == 99  # type: ignore[index]
    assert gh.sent("GRAPHQL", "convertPullRequestToDraft") == [{"id": "PR_node_99"}]


def test_set_draft_converts_ready_pr_to_draft() -> None:
    gh = FakeGH(
        {
            ("GRAPHQL", "query"): pr_graphql_response(number=42, id="PR_node_42"),
            ("GRAPHQL", "convertPullRequestToDraft"): {},
        }
    )
    # no open PR for the branch: falls back to the all-states lookup
    gh.routes[("GET", PULLS)] = lambda params: [] if params["state"] == "open" else [rest_pr(42)]

    assert set_draft(gh) is True  # type: ignore[arg-type]
    assert gh.sent("GRAPHQL", "convertPullRequestToDraft") == [{"id": "PR_node_42"}]


def test_set_draft_no_op_when_already_draft() -> None:
    gh = FakeGH({("GET", PULLS): [rest_pr(42)], ("GRAPHQL", "query"): pr_graphql_response(number=42, isDraft=True)})

    assert set_draft(gh) is False  # type: ignore[arg-type]
    assert gh.sent("GRAPHQL", "convertPullRequestToDraft") == []


def test_create_without_agent_makes_no_follow_up_calls(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: [])
    gh = FakeGH({("POST", PULLS): {"number": 7, "html_url": "u"}})

    create(gh, "main", [])  # type: ignore[arg-type]

    assert len(gh.calls) == 1


def test_create_appends_agent_session_note_when_detected(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: [])
    gh = FakeGH(
        {
            ("POST", PULLS): {"number": 7, "html_url": "u"},
            ("GET", f"{PULLS}/7"): rest_pr(7, body="PR body"),
            ("PATCH", f"{PULLS}/7"): {},
        }
    )

    create(gh, "main", [])  # type: ignore[arg-type]

    assert gh.sent("PATCH", f"{PULLS}/7") == [{"body": "PR body\n\nClaude Session: https://claude.ai/code/session_abc123"}]


def _fake_push_assets(monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.push_assets",
        lambda owner, repo, branch, paths, **kwargs: [f"https://github.com/{owner}/{repo}/blob/pr-assets/{branch}/{i:02d}-{p.split('/')[-1]}?raw=true" for i, p in enumerate(paths)],
    )


def test_add_files_pushes_and_appends_attachments_section(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    gh = FakeGH({**view_routes(7), ("PATCH", f"{PULLS}/7"): {}})

    add_files(gh, "owner", "repo", "feature-x", ["/tmp/report.pdf"])  # type: ignore[arg-type]

    body = gh.sent("PATCH", f"{PULLS}/7")[0]["body"]  # type: ignore[index]
    assert "## Attachments" in body
    assert "[report.pdf]" in body
    assert "existing body" in body


def test_add_attachments_edits_pr_body_once_for_both_kinds(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    gh = FakeGH({**view_routes(7), ("PATCH", f"{PULLS}/7"): {}})

    add_attachments(gh, "owner", "repo", "feature-x", screenshot_paths=["/tmp/shot.png"], file_paths=["/tmp/report.pdf"])  # type: ignore[arg-type]

    assert len(gh.sent("GET", f"{PULLS}/7")) == 1
    edits = gh.sent("PATCH", f"{PULLS}/7")
    assert len(edits) == 1
    body = edits[0]["body"]  # type: ignore[index]
    assert "## Screenshots" in body
    assert "## Attachments" in body
    assert body.index("## Screenshots") < body.index("## Attachments")


def test_update_appends_both_screenshots_and_files(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    gh = FakeGH({**view_routes(7), ("PATCH", f"{PULLS}/7"): {}})

    update(gh, "owner", "repo", "feature-x", screenshot_paths=["/tmp/shot.png"], file_paths=["/tmp/report.pdf"])  # type: ignore[arg-type]

    body = gh.sent("PATCH", f"{PULLS}/7")[0]["body"]  # type: ignore[index]
    assert "## Screenshots" in body
    assert "## Attachments" in body
    assert body.index("## Screenshots") < body.index("## Attachments")


def test_update_sends_title_only_when_only_a_title_is_given() -> None:
    gh = FakeGH({**view_routes(7), ("PATCH", f"{PULLS}/7"): {}})

    update(gh, "owner", "repo", "feature-x", title="New title")  # type: ignore[arg-type]

    assert gh.sent("PATCH", f"{PULLS}/7") == [{"title": "New title"}]


def test_update_without_changes_does_not_patch() -> None:
    gh = FakeGH(view_routes(7))
    update(gh, "owner", "repo", "feature-x")  # type: ignore[arg-type]
    assert gh.sent("PATCH") == []


def test_update_skips_session_note_if_already_in_current_title(monkeypatch) -> None:
    """`update()` fetches the PR's title (not just its body) so an agent session already
    referenced there -- not just in the body being replaced -- is still recognized."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    gh = FakeGH(
        {
            **view_routes(7, title="Fix bug (Claude Session: https://claude.ai/code/session_abc123)", body="old body"),
            ("PATCH", f"{PULLS}/7"): {},
        }
    )

    update(gh, "owner", "repo", "feature-x", description="new body")  # type: ignore[arg-type]

    assert gh.sent("PATCH", f"{PULLS}/7") == [{"body": "new body"}]


def test_update_with_pr_id_reads_that_pr_and_ignores_the_passed_in_branch(monkeypatch) -> None:
    """With `--pr-id`, `update()` must resolve the given PR directly (not the current
    branch's), and namespace any uploaded screenshots/files under that PR's actual head
    branch -- not the `branch` argument the caller passed in, which may be stale or just
    plain wrong if that PR's branch isn't checked out locally at all.
    """
    _fake_push_assets(monkeypatch)
    gh = FakeGH({("GET", f"{PULLS}/99"): rest_pr(99, head="feature-y"), ("PATCH", f"{PULLS}/99"): {}})

    update(gh, "owner", "repo", "wrong-branch", screenshot_paths=["/tmp/shot.png"], pr_id=99)  # type: ignore[arg-type]

    assert gh.sent("GET", PULLS) == []
    body = gh.sent("PATCH", f"{PULLS}/99")[0]["body"]  # type: ignore[index]
    assert "feature-y" in body
    assert "wrong-branch" not in body


def test_comment_with_screenshots_and_files_builds_both_sections(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    gh = FakeGH({**view_routes(7), ("POST", "repos/owner/repo/issues/7/comments"): {}})

    comment_with_screenshots(gh, "owner", "repo", "feature-x", "Fixed", ["/tmp/shot.png"], ["/tmp/report.pdf"])  # type: ignore[arg-type]

    body = gh.sent("POST", "repos/owner/repo/issues/7/comments")[0]["body"]  # type: ignore[index]
    assert "Fixed" in body
    assert "## Screenshots" in body
    assert "## Attachments" in body


def test_comment_with_screenshots_with_pr_id_targets_that_pr_number() -> None:
    gh = FakeGH({("POST", "repos/owner/repo/issues/99/comments"): {}})

    comment_with_screenshots(gh, "owner", "repo", "feature-x", "Fixed", [], pr_id=99)  # type: ignore[arg-type]

    assert gh.calls == [("POST", "repos/owner/repo/issues/99/comments", {"body": "Fixed"})]


def test_comment_with_screenshots_appends_agent_session_note_when_detected(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    gh = FakeGH({**view_routes(7), ("POST", "repos/owner/repo/issues/7/comments"): {}})

    comment_with_screenshots(gh, "owner", "repo", "feature-x", "Fixed", [], [])  # type: ignore[arg-type]

    body = gh.sent("POST", "repos/owner/repo/issues/7/comments")[0]["body"]  # type: ignore[index]
    assert body == "Fixed\n\nClaude Session: https://claude.ai/code/session_abc123"


def test_failed_run_ids_dedupes_and_ignores_non_failing_checks() -> None:
    checks = [COMPLETED_SUCCESS_CHECK_RUN, FAILED_CHECK_RUN, FAILED_CHECK_RUN_SAME_RUN]
    assert failed_run_ids(checks) == [34882319228]


def test_failed_run_ids_skips_status_context_with_no_details_url() -> None:
    assert failed_run_ids([FAILED_STATUS_CONTEXT]) == []


def test_failed_run_ids_empty_when_nothing_failed() -> None:
    assert failed_run_ids([COMPLETED_SUCCESS_CHECK_RUN, COMPLETED_SKIPPED_CHECK_RUN]) == []


def test_retry_hint_names_the_command(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert "bdt pr retry" in retry_hint()


def test_retry_hint_tells_claude_code_to_run_it(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    assert "bdt pr retry" in retry_hint()



RERUN_99 = "repos/owner/repo/actions/runs/34882319228/rerun-failed-jobs"


def _pr_with(checks: list[dict]):
    return lambda gh, pr_id=None: {"statusCheckRollup": checks}


def test_retry_reruns_each_distinct_failed_run(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", _pr_with([FAILED_CHECK_RUN, FAILED_CHECK_RUN_SAME_RUN]))
    gh = FakeGH({("POST", RERUN_99): None})

    retry(gh)  # type: ignore[arg-type]

    assert [c[1] for c in gh.calls] == [RERUN_99]


def test_retry_with_pr_id_reads_that_pr_instead_of_the_current_branch(monkeypatch) -> None:
    seen: list[int | None] = []
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: seen.append(pr_id) or {"statusCheckRollup": [FAILED_CHECK_RUN]})

    retry(FakeGH({("POST", RERUN_99): None}), pr_id=99)  # type: ignore[arg-type]

    assert seen == [99]


def test_retry_exits_when_no_failed_run_found(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", _pr_with([COMPLETED_SUCCESS_CHECK_RUN]))

    with pytest.raises(SystemExit):
        retry(FakeGH())  # type: ignore[arg-type]


def test_retry_exits_on_rerun_failure(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", _pr_with([FAILED_CHECK_RUN]))

    with pytest.raises(SystemExit, match="already in progress"):
        retry(FakeGH({("POST", RERUN_99): SystemExit("run is already in progress")}))  # type: ignore[arg-type]


def test_retry_still_attempts_remaining_runs_after_one_fails(monkeypatch, capsys) -> None:
    """A failure reranning one run shouldn't stop bdt from attempting the others."""
    # Make the two failed checks belong to *different* runs so both get a rerun attempt.
    other_run_check = {**FAILED_CHECK_RUN_SAME_RUN, "detailsUrl": "https://github.com/owner/repo/actions/runs/999/job/1"}
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", _pr_with([FAILED_CHECK_RUN, other_run_check]))
    gh = FakeGH({("POST", RERUN_99): SystemExit("run is already in progress"), ("POST", "repos/owner/repo/actions/runs/999/rerun-failed-jobs"): None})

    with pytest.raises(SystemExit) as exc_info:
        retry(gh)  # type: ignore[arg-type]

    assert [c[1].split("/")[-2] for c in gh.calls] == ["34882319228", "999"]
    assert "999" in capsys.readouterr().out
    assert "34882319228" in str(exc_info.value)


RUNS = "repos/owner/repo/actions/runs"
# A workflow run triggered by a push to a branch (as opposed to a `pull_request` trigger),
# shaped like the REST `actions/runs` endpoint returns it.
REST_DEPLOY_RUN = {
    "id": 555,
    "name": "Deploy",
    "display_title": "feat: something",
    "status": "completed",
    "conclusion": "success",
    "html_url": "https://github.com/owner/repo/actions/runs/555",
    "head_branch": "main",
}


def test_get_workflow_runs_for_branch_filters_by_branch_and_push_event() -> None:
    gh = FakeGH({("GET", RUNS): {"workflow_runs": [REST_DEPLOY_RUN]}})

    runs = get_workflow_runs_for_branch(gh, "main", limit=3)  # type: ignore[arg-type]

    assert runs == [DEPLOY_RUN]
    assert gh.sent("GET", RUNS) == [{"branch": "main", "event": "push", "per_page": 3}]


def test_get_workflow_runs_for_branch_exits_on_api_failure() -> None:
    with pytest.raises(SystemExit):
        get_workflow_runs_for_branch(FakeGH({("GET", RUNS): SystemExit("HTTP 401")}), "main")  # type: ignore[arg-type]


def test_print_failed_step_logs_prints_failed_steps_and_job_log(capsys) -> None:
    gh = FakeGH(
        {
            ("GET", f"{RUNS}/556/jobs"): {
                "jobs": [
                    {"id": 1, "name": "ok", "conclusion": "success", "steps": []},
                    {"id": 2, "name": "build", "conclusion": "failure", "steps": [{"name": "Run tests", "conclusion": "failure"}, {"name": "Setup", "conclusion": "success"}]},
                ]
            },
            ("GET", "repos/owner/repo/actions/jobs/2/logs"): FakeResponse("line1\nboom\n"),
        }
    )

    print_failed_step_logs(gh, 556)  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "Failed steps (run 556)" in out
    assert "build -- failed step(s): Run tests" in out
    assert "boom" in out
    assert not any("jobs/1/" in c[1] for c in gh.calls)


def test_print_failed_step_logs_is_best_effort(capsys) -> None:
    print_failed_step_logs(FakeGH({("GET", f"{RUNS}/556/jobs"): SystemExit("nope")}), 556)  # type: ignore[arg-type]

    assert "no failed steps with logs" in capsys.readouterr().out


def test_print_failed_step_logs_without_downloadable_log(capsys) -> None:
    gh = FakeGH(
        {
            ("GET", f"{RUNS}/556/jobs"): {"jobs": [{"id": 2, "name": "build", "conclusion": "failure", "steps": []}]},
            ("GET", "repos/owner/repo/actions/jobs/2/logs"): FakeResponse("", 410),
        }
    )

    print_failed_step_logs(gh, 556)  # type: ignore[arg-type]

    assert "(no log available)" in capsys.readouterr().out


def _runs_gh(runs_json: list[dict], **extra) -> FakeGH:
    """A fake whose `actions/runs` answers `runs_json` (given in the `DEPLOY_RUN` shape)."""
    rest = [{"id": r["databaseId"], "name": r["workflowName"], "status": r["status"], "conclusion": r["conclusion"], "html_url": r["url"], "head_branch": "main"} for r in runs_json]
    return FakeGH({("GET", RUNS): {"workflow_runs": rest}, **extra})


def _no_logs(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.print_failed_step_logs", lambda gh, run_id: None)


def test_run_watch_deploy_prints_completed_run_and_returns_without_wait(capsys) -> None:
    run_watch_deploy(_runs_gh([DEPLOY_RUN]), "main", wait=False)  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "Deploy #555" in out
    assert "SUCCESS" in out


def test_run_watch_deploy_no_runs_prints_message_and_returns(capsys) -> None:
    run_watch_deploy(_runs_gh([]), "main", wait=False)  # type: ignore[arg-type]

    assert "No workflow runs found on 'main'" in capsys.readouterr().out


def test_run_watch_deploy_exits_1_on_failed_run(monkeypatch) -> None:
    _no_logs(monkeypatch)
    failed_run = {**DEPLOY_RUN, "databaseId": 556, "conclusion": "failure"}

    with pytest.raises(SystemExit) as exc_info:
        run_watch_deploy(_runs_gh([failed_run]), "main", wait=False)  # type: ignore[arg-type]

    assert exc_info.value.code == 1


def test_run_watch_deploy_wait_stops_and_reports_pending_approval(capsys) -> None:
    """--wait must not poll forever when the only deploy workflow run is paused on a
    deployment protection rule (status "waiting") -- it never completes on its own.
    """
    waiting_run = {**DEPLOY_RUN, "databaseId": 557, "status": "waiting", "conclusion": None}

    with pytest.raises(SystemExit) as exc_info:
        run_watch_deploy(_runs_gh([waiting_run]), "main", wait=True)  # type: ignore[arg-type]

    assert exc_info.value.code == EXIT_NEEDS_APPROVAL
    assert "needs a reviewer" in capsys.readouterr().out


def test_run_watch_deploy_wait_exits_1_not_2_when_another_run_already_failed(monkeypatch) -> None:
    """A run stuck on approval must not mask an already-failed run in the same batch."""
    _no_logs(monkeypatch)
    failed_run = {**DEPLOY_RUN, "databaseId": 556, "workflowName": "CI", "conclusion": "failure"}
    waiting_run = {**DEPLOY_RUN, "databaseId": 557, "workflowName": "Deploy", "status": "waiting", "conclusion": None}

    with pytest.raises(SystemExit) as exc_info:
        run_watch_deploy(_runs_gh([failed_run, waiting_run]), "main", wait=True)  # type: ignore[arg-type]

    assert exc_info.value.code == 1


def _pr_view(checks: list[dict]) -> dict:
    return {"number": 7, "title": "feat: x", "baseRefName": "main", "mergeable": "MERGEABLE", "statusCheckRollup": checks, "isDraft": False}


def test_run_prints_deploy_hint_after_checks_pass(monkeypatch, capsys) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: _pr_view([COMPLETED_SUCCESS_CHECK_RUN]))

    run(_runs_gh([DEPLOY_RUN]), wait=False)  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "bdt pr watch-deploy" in out
    assert "main" in out


def test_run_prints_deploy_hint_when_pr_has_no_checks_at_all(monkeypatch, capsys) -> None:
    """Regression: a PR with no `statusCheckRollup` entries at all (e.g. this repo's only
    workflow triggers on a push to the target branch, not on `pull_request`) is itself a
    settled state -- exactly when the hint is most useful -- so it must still be checked,
    not skipped just because there were no PR-triggered checks to report."""
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: _pr_view([]))

    run(_runs_gh([DEPLOY_RUN]), wait=False)  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "no checks found" in out
    assert "bdt pr watch-deploy" in out


def test_run_skips_deploy_hint_when_checks_still_pending_without_wait(monkeypatch, capsys) -> None:
    """Regression: without --wait, a still-pending check must not be mistaken for
    'checks succeeded' -- the hint should only ever follow a genuinely settled result."""
    hint_calls: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: _pr_view([{"__typename": "CheckRun", "status": "IN_PROGRESS", "name": "build"}]))
    monkeypatch.setattr("bmsdna.devtools.gh_pr.deploy_run_hint", lambda gh, branch: hint_calls.append(branch) or "should not print")

    run(FakeGH(), wait=False)  # type: ignore[arg-type]

    assert hint_calls == []
    assert "should not print" not in capsys.readouterr().out


def test_run_skips_deploy_hint_when_check_failed(monkeypatch, capsys) -> None:
    hint_calls: list[str] = []
    failing = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE", "name": "build"}
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: _pr_view([failing]))
    monkeypatch.setattr("bmsdna.devtools.gh_pr.deploy_run_hint", lambda gh, branch: hint_calls.append(branch) or "should not print")

    with pytest.raises(SystemExit):
        run(FakeGH(), wait=False)  # type: ignore[arg-type]

    assert hint_calls == []
    assert "should not print" not in capsys.readouterr().out


def test_has_build_policy_true_when_status_checks_required() -> None:
    gh = FakeGH({("GET", "repos/owner/repo/branches/main/protection"): {"required_status_checks": {"contexts": ["build"]}}})
    assert has_build_policy(gh, "main") is True  # type: ignore[arg-type]


def test_has_build_policy_fails_open_on_api_error() -> None:
    """Best-effort (fails open): an unprotected branch / missing permission is just False."""
    gh = FakeGH({("GET", "repos/owner/repo/branches/main/protection"): SystemExit("Branch not protected")})
    assert has_build_policy(gh, "main") is False  # type: ignore[arg-type]


# A workflow run triggered by a push to a branch (as opposed to a `pull_request` trigger),
# shaped like `gh run list --branch main --event push --json ...` actually returns.
DEPLOY_RUN = {
    "databaseId": 555,
    "name": "Deploy",
    "workflowName": "Deploy",
    "status": "completed",
    "conclusion": "success",
    "url": "https://github.com/owner/repo/actions/runs/555",
    "headBranch": "main",
}


def test_latest_per_workflow_keeps_highest_id_per_workflow() -> None:
    runs = [
        {"databaseId": 1, "workflowName": "CI"},
        {"databaseId": 3, "workflowName": "CI"},
        {"databaseId": 2, "workflowName": "Deploy"},
    ]

    result = latest_per_workflow(runs)

    assert {r["databaseId"] for r in result} == {3, 2}


def test_deploy_run_hint_none_when_no_runs_on_target(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_workflow_runs_for_branch", lambda gh, branch, limit=5: [])
    assert deploy_run_hint("gh", "main") is None


def test_deploy_run_hint_mentions_branch_and_run_when_found(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_workflow_runs_for_branch", lambda gh, branch, limit=5: [DEPLOY_RUN])

    hint = deploy_run_hint("gh", "main")

    assert hint is not None
    assert "main" in hint
    assert "#555" in hint
    assert "bdt pr watch-deploy" in hint


def test_deploy_run_hint_fails_open_on_error(monkeypatch) -> None:
    def raise_exit(gh, branch, limit=5):
        raise SystemExit("boom")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_workflow_runs_for_branch", raise_exit)

    assert deploy_run_hint("gh", "main") is None


def test_push_assets_bounds_fetch_and_push_with_a_longer_upload_timeout(monkeypatch) -> None:
    """Regression: `git fetch`/`git push` of actual screenshot blob content on the
    `pr-assets` branch must get more time than a plain metadata call (CLI_UPLOAD_TIMEOUT_SECS,
    not the tighter CLI_TIMEOUT_SECS) -- a large/slow transfer shouldn't be aborted just
    because it's slower than a `gh pr view`.
    """
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:2] == ["git", "ls-remote"]:
            return MagicMock(returncode=0, stdout="abc123\trefs/heads/pr-assets\n", stderr="")
        if cmd[:2] == ["git", "hash-object"]:
            return MagicMock(returncode=0, stdout="blobsha\n", stderr="")
        if cmd[:2] == ["git", "write-tree"]:
            return MagicMock(returncode=0, stdout="treesha\n", stderr="")
        if cmd[:2] == ["git", "commit-tree"]:
            return MagicMock(returncode=0, stdout="commitsha\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    push_assets("owner", "repo", "feature-x", ["/tmp/shot.png"])

    fetch_kwargs = next(kwargs for cmd, kwargs in calls if cmd[:2] == ["git", "fetch"])
    push_kwargs = next(kwargs for cmd, kwargs in calls if cmd[:2] == ["git", "push"])
    ls_remote_kwargs = next(kwargs for cmd, kwargs in calls if cmd[:2] == ["git", "ls-remote"])

    assert fetch_kwargs["timeout"] == CLI_UPLOAD_TIMEOUT_SECS
    assert push_kwargs["timeout"] == CLI_UPLOAD_TIMEOUT_SECS
    assert ls_remote_kwargs["timeout"] == CLI_TIMEOUT_SECS



# -- get_pr_body / link_issue_to_pr -----------------------------------------


def test_get_pr_body_returns_number_and_body() -> None:
    assert get_pr_body(FakeGH(view_routes(7, body="some body"))) == (7, "some body")  # type: ignore[arg-type]


def test_get_pr_body_missing_body_is_empty_string() -> None:
    gh = FakeGH({("GET", PULLS): [rest_pr(7)], ("GET", f"{PULLS}/7"): {**rest_pr(7), "body": None}})
    assert get_pr_body(gh) == (7, "")  # type: ignore[arg-type]


def test_link_issue_to_pr_appends_fixes_keyword() -> None:
    gh = FakeGH({("PATCH", f"{PULLS}/7"): {}})

    new_body = link_issue_to_pr(gh, 7, "existing body", 42)  # type: ignore[arg-type]

    edited_body = gh.sent("PATCH", f"{PULLS}/7")[0]["body"]  # type: ignore[index]
    assert "existing body" in edited_body
    assert "Fixes #42" in edited_body
    assert new_body == edited_body


def test_link_issue_to_pr_noop_when_body_already_has_closing_keyword() -> None:
    gh = FakeGH()  # any request would raise: the body already closes the issue

    body = "This closes #42 already."
    assert link_issue_to_pr(gh, 7, body, 42) == body  # type: ignore[arg-type]
    assert gh.calls == []


def test_link_issue_to_pr_still_appends_when_body_only_has_a_plain_mention() -> None:
    """Regression: a bare `#42` mention elsewhere in the body (e.g. "see #42 for background")
    isn't a real GitHub closing keyword -- must not be mistaken for one already being present.
    """
    gh = FakeGH({("PATCH", f"{PULLS}/7"): {}})

    new_body = link_issue_to_pr(gh, 7, "See #42 for background, not fixing it here.", 42)  # type: ignore[arg-type]

    assert len(gh.sent("PATCH", f"{PULLS}/7")) == 1
    assert "Fixes #42" in new_body


def test_link_issue_to_pr_raises_on_api_failure() -> None:
    gh = FakeGH({("PATCH", f"{PULLS}/7"): SystemExit("permission denied")})

    with pytest.raises(SystemExit, match="permission denied"):
        link_issue_to_pr(gh, 7, "body", 42)  # type: ignore[arg-type]


def test_get_pr_pages_through_more_than_100_check_contexts() -> None:
    def ctx(i: int) -> dict:
        return {"__typename": "CheckRun", "name": f"job-{i}", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "u", "checkSuite": None}

    first = pr_graphql_response()
    first["repository"]["pullRequest"]["statusCheckRollup"] = {"contexts": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [ctx(i) for i in range(100)]}}
    page2 = {"repository": {"pullRequest": {"statusCheckRollup": {"contexts": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [ctx(100), ctx(101)]}}}}}
    gh = FakeGH({("GRAPHQL", "query"): first, ("GRAPHQL", "query ContextsPage"): page2})

    pr = get_pr(gh, 7, fields="statusCheckRollup")  # type: ignore[arg-type]

    assert len(pr["statusCheckRollup"]) == 102
    assert gh.sent("GRAPHQL", "query ContextsPage") == [{"owner": "owner", "repo": "repo", "number": 7, "after": "c1"}]


def test_status_context_gets_a_name_and_details_url() -> None:
    gh = FakeGH({("GRAPHQL", "query"): pr_graphql_response()})

    ctx = get_pr(gh, 7, fields="statusCheckRollup")["statusCheckRollup"][1]  # type: ignore[arg-type]

    assert ctx["name"] == "ext" and ctx["detailsUrl"] == "t"
    assert check_label(ctx) == "ext"


def test_head_branch_is_one_rest_call() -> None:
    gh = FakeGH({("GET", f"{PULLS}/99"): rest_pr(99, head="topic")})

    assert head_branch(gh, 99) == "topic"  # type: ignore[arg-type]
    assert [c[:2] for c in gh.calls] == [("GET", f"{PULLS}/99")]


def test_create_accepts_description_as_an_alias_of_body(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr._branch_commits", lambda target: [])
    gh = FakeGH({("POST", PULLS): {"number": 7, "html_url": "u"}})

    create(gh, "main", ["--description", "full text", "-t", "T"])  # type: ignore[arg-type]

    sent = gh.sent("POST", PULLS)[0]
    assert sent["body"] == "full text" and sent["title"] == "T"  # type: ignore[index]
