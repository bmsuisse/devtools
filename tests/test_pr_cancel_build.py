"""`bdt pr cancel-build` / `requeue-build`, the `CANCELLED BUILD:` comment and its display in `pr info` / `pr status`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bmsdna.devtools import pr_build, pr_cancel, pr_info
from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote
from tests.test_gh_pr import FAILED_CHECK_RUN, COMPLETED_SUCCESS_CHECK_RUN, FakeGH

REMOTE = AdoRemote(org="myorg", project="MyProj", repo="myrepo")
PR = {"pullRequestId": 42, "title": "feat", "sourceRefName": "refs/heads/feature-x"}
runner = CliRunner()


# -- the comment ---------------------------------------------------------------


def test_comment_body_starts_with_the_prefix_and_lists_the_builds() -> None:
    body = pr_cancel.comment_body("  wrong branch deployed ", [("CI", 100, "https://x/100"), ("E2E", 101, "https://x/101")])

    assert body.startswith("CANCELLED BUILD: wrong branch deployed\n\n")
    assert "- CI #100: https://x/100" in body and "- E2E #101: https://x/101" in body


def test_parse_reads_back_the_reason_and_build_ids() -> None:
    body = pr_cancel.comment_body("wrong branch", [("CI", 100, "https://x/100")])

    assert pr_cancel.parse(body + "\n\nClaude Session: https://claude.ai/code/s") == ("wrong branch", {100})


def test_parse_ignores_other_comments() -> None:
    assert pr_cancel.parse("LGTM, but CANCELLED BUILD: is not at the start") is None


def test_hand_written_comment_is_about_any_build() -> None:
    assert pr_cancel.parse("CANCELLED BUILD: infra down") == ("infra down", set())


def test_reason_is_one_clean_line() -> None:
    reason, _ = pr_cancel.parse("CANCELLED BUILD: evil\x1b]8;;http://x\x07\n  text\n\nrest") or ("", set())

    assert reason == "evil]8;;http://x text"


def test_latest_reason_is_the_newest_about_a_cancelled_build() -> None:
    comments = [
        pr_cancel.comment_body("first", [("CI", 100, "u")]),
        "unrelated",
        pr_cancel.comment_body("second", [("CI", 101, "u")]),
    ]

    assert pr_cancel.latest_reason(comments, {100, 101}) == "second"
    assert pr_cancel.latest_reason(comments, {100}) == "first"  # build 101 isn't the cancelled one any more
    assert pr_cancel.latest_reason(comments, {999}) is None
    assert pr_cancel.latest_reason(["unrelated"], {100}) is None


def test_notice_without_a_reason_says_so() -> None:
    assert "no 'CANCELLED BUILD:' comment" in pr_cancel.cancelled_notice(None)
    assert pr_cancel.cancelled_notice("why") == "Build cancelled: why"


# -- Azure DevOps ---------------------------------------------------------------

RUNNING = {"id": 102, "status": "inProgress", "result": None, "definition": {"id": 3, "name": "Deploy"}, "sourceBranch": "refs/pull/42/merge"}
CANCELLED = {"id": 103, "status": "completed", "result": "canceled", "definition": {"id": 1, "name": "CI"}, "sourceBranch": "refs/pull/42/merge", "parameters": '{"a": "b"}'}
FAILED = {"id": 104, "status": "completed", "result": "failed", "definition": {"id": 2, "name": "Lint"}, "sourceBranch": "refs/pull/42/merge"}
SUCCEEDED = {"id": 105, "status": "completed", "result": "succeeded", "definition": {"id": 4, "name": "Docs"}, "sourceBranch": "refs/pull/42/merge"}


def _plumbing(monkeypatch, builds: list[dict]) -> MagicMock:
    session = MagicMock()
    monkeypatch.setattr("bmsdna.devtools.pr_build.ado_session", lambda pat: session)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda *a, **k: PR)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_builds_for_pr", lambda *a, **k: builds)
    return session


def test_cancel_cancels_running_builds_and_posts_the_reason(monkeypatch, capsys) -> None:
    session = _plumbing(monkeypatch, [RUNNING, CANCELLED])
    cancelled: list[int] = []
    comments: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.pr_build.cancel_running_build", lambda s, r, build_id: cancelled.append(build_id))
    monkeypatch.setattr("bmsdna.devtools.pr_build.add_comment", lambda s, r, pr_id, content: comments.append(f"{pr_id}: {content}"))

    pr_build.cancel(REMOTE, "pat", None, "wrong branch", source_branch="feature-x")

    assert cancelled == [102]
    assert len(comments) == 1
    assert comments[0].startswith("42: CANCELLED BUILD: wrong branch")
    assert "#102" in comments[0] and "_build/results?buildId=102" in comments[0]
    assert "bdt pr requeue-build" in capsys.readouterr().out
    assert session is not None


def test_cancel_marks_the_build_cancelling_through_the_build_api() -> None:
    session = MagicMock()

    pr_build.cancel_running_build(session, REMOTE, 102)

    call = session.patch.call_args
    assert call.args[0] == "https://dev.azure.com/myorg/MyProj/_apis/build/builds/102"
    assert call.kwargs["json"] == {"status": "cancelling"}


def test_cancel_without_a_running_build_exits_and_posts_nothing(monkeypatch) -> None:
    _plumbing(monkeypatch, [CANCELLED, FAILED])
    monkeypatch.setattr("bmsdna.devtools.pr_build.add_comment", lambda *a: pytest.fail("no comment without a cancelled build"))

    with pytest.raises(SystemExit, match="No running build"):
        pr_build.cancel(REMOTE, "pat", None, "why", source_branch="feature-x")


def test_cancel_requires_a_reason(monkeypatch) -> None:
    with pytest.raises(SystemExit, match="reason is required"):
        pr_build.cancel(REMOTE, "pat", None, "  ", source_branch="feature-x")


def test_cancel_does_not_claim_a_cancellation_that_failed(monkeypatch) -> None:
    import requests

    _plumbing(monkeypatch, [RUNNING])

    def fail(*a):
        raise requests.ConnectionError("down")

    monkeypatch.setattr("bmsdna.devtools.pr_build.cancel_running_build", fail)
    monkeypatch.setattr("bmsdna.devtools.pr_build.add_comment", lambda *a: pytest.fail("nothing was cancelled"))

    with pytest.raises(SystemExit, match="Failed to cancel"):
        pr_build.cancel(REMOTE, "pat", None, "why", source_branch="feature-x")


def test_requeue_queues_cancelled_and_failed_pipelines_only(monkeypatch, capsys) -> None:
    _plumbing(monkeypatch, [CANCELLED, FAILED, SUCCEEDED, RUNNING])
    queued: list[int] = []
    monkeypatch.setattr("bmsdna.devtools.pr_build.queue_build", lambda s, r, b: queued.append(b["id"]) or {"id": b["id"] + 100})

    pr_build.requeue(REMOTE, "pat", None, source_branch="feature-x")

    assert sorted(queued) == [103, 104]
    assert "Queued build #203 (CI)" in capsys.readouterr().out


def test_requeue_all_includes_succeeded_builds(monkeypatch) -> None:
    _plumbing(monkeypatch, [SUCCEEDED])
    queued: list[int] = []
    monkeypatch.setattr("bmsdna.devtools.pr_build.queue_build", lambda s, r, b: queued.append(b["id"]) or {"id": 1})

    with pytest.raises(SystemExit, match="nothing to requeue"):
        pr_build.requeue(REMOTE, "pat", None, source_branch="feature-x")
    pr_build.requeue(REMOTE, "pat", None, include_succeeded=True, source_branch="feature-x")

    assert queued == [105]


def test_requeue_while_running_says_to_cancel_first(monkeypatch) -> None:
    _plumbing(monkeypatch, [RUNNING])

    with pytest.raises(SystemExit, match="cancel-build"):
        pr_build.requeue(REMOTE, "pat", None, source_branch="feature-x")


def test_queue_build_reuses_ref_and_parameters_of_the_old_build() -> None:
    session = MagicMock()
    session.post.return_value.json.return_value = {"id": 300}

    assert pr_build.queue_build(session, REMOTE, CANCELLED) == {"id": 300}

    call = session.post.call_args
    assert call.args[0] == "https://dev.azure.com/myorg/MyProj/_apis/build/builds"
    assert call.kwargs["json"] == {"definition": {"id": 1}, "sourceBranch": "refs/pull/42/merge", "parameters": '{"a": "b"}'}


def _session_with_comments(texts: list[str]) -> MagicMock:
    session = MagicMock()
    session.get.return_value.json.return_value = {
        "value": [
            {"comments": [{"content": t, "commentType": "text", "publishedDate": f"2026-01-0{i + 1}"}]} for i, t in enumerate(texts)
        ]
        + [{"comments": [{"content": "CANCELLED BUILD: system noise", "commentType": "system", "publishedDate": "2026-02-01"}]}]
    }
    return session


def test_cancel_reason_reads_the_pr_comments_for_the_cancelled_build() -> None:
    session = _session_with_comments([pr_cancel.comment_body("deploy raced", [("CI", 103, "u")])])

    assert pr_build.cancel_reason(session, REMOTE, 42, [CANCELLED, FAILED]) == "deploy raced"


def test_cancel_reason_is_none_without_a_cancelled_build_and_skips_the_request() -> None:
    session = MagicMock()

    assert pr_build.cancel_reason(session, REMOTE, 42, [FAILED]) is None
    session.get.assert_not_called()


def test_status_shows_why_the_build_was_cancelled_and_exits_1(monkeypatch, capsys) -> None:
    _plumbing(monkeypatch, [CANCELLED, SUCCEEDED])
    monkeypatch.setattr("bmsdna.devtools.pr_build.print_build", lambda *a: None)
    monkeypatch.setattr("bmsdna.devtools.pr_build.deploy_build_hint", lambda *a: None)
    monkeypatch.setattr("bmsdna.devtools.pr_build.cancel_reason", lambda *a: "deploy raced")

    with pytest.raises(SystemExit) as exc_info:
        pr_build.run(REMOTE, "pat", None, wait=False, source_branch="feature-x")

    out = capsys.readouterr().out
    assert exc_info.value.code == 1
    assert "Build cancelled: deploy raced" in out and "bdt pr requeue-build" in out


# -- pr info --------------------------------------------------------------------


def test_info_text_shows_the_cancel_reason() -> None:
    from bmsdna.devtools.cli import _info_text

    info = pr_info.PrInfo(1, "t", "https://x/1", "open", False, pr_info.BUILD_CANCELLED, cancel_reason="deploy raced")

    assert "(open, build: cancelled)" in _info_text(info)
    assert "Build cancelled: deploy raced" in _info_text(info)
    assert info.to_json_dict()["cancel_reason"] == "deploy raced"


def test_ado_info_reports_cancelled_with_the_reason(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.get_builds_for_pr", lambda *a: [CANCELLED, SUCCEEDED])
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.find_pending_approvals", lambda *a: [])
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.cancel_reason", lambda *a: "deploy raced")

    info = pr_info.ado_info(MagicMock(), PR, REMOTE)

    assert (info.build, info.cancel_reason) == (pr_info.BUILD_CANCELLED, "deploy raced")


def test_failed_beats_cancelled_in_the_aggregate() -> None:
    assert pr_info.ado_build_state([CANCELLED, FAILED], False) == pr_info.BUILD_FAILING


# -- GitHub ---------------------------------------------------------------------

RUN_URL = "https://github.com/owner/repo/actions/runs/{}/job/1"
RUNNING_CHECK = {"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS", "workflowName": "CI", "detailsUrl": RUN_URL.format(7)}
CANCELLED_CHECK = {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "CANCELLED", "workflowName": "CI", "detailsUrl": RUN_URL.format(8)}
COMMENTS = "repos/owner/repo/issues/9/comments"


GH = FakeGH


def _gh_pr(monkeypatch, checks: list[dict]) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None, **k: {"number": 9, "statusCheckRollup": checks})


def test_github_cancel_cancels_running_runs_and_posts_the_reason(monkeypatch) -> None:
    _gh_pr(monkeypatch, [RUNNING_CHECK, COMPLETED_SUCCESS_CHECK_RUN])
    gh = GH({("POST", "repos/owner/repo/actions/runs/7/cancel"): None, ("POST", COMMENTS): {}})

    from bmsdna.devtools import gh_pr

    gh_pr.cancel(gh, "wrong branch")  # type: ignore[arg-type]

    assert [c[1] for c in gh.calls] == ["repos/owner/repo/actions/runs/7/cancel", COMMENTS]
    body = gh.calls[1][2]["body"]  # type: ignore[index]
    assert body.startswith("CANCELLED BUILD: wrong branch") and "#7" in body


def test_github_cancel_without_a_running_run_exits(monkeypatch) -> None:
    from bmsdna.devtools import gh_pr

    _gh_pr(monkeypatch, [COMPLETED_SUCCESS_CHECK_RUN])

    with pytest.raises(SystemExit, match="No running workflow run"):
        gh_pr.cancel(GH(), "why")  # type: ignore[arg-type]


def test_github_requeue_reruns_cancelled_and_failed_runs_in_full(monkeypatch) -> None:
    from bmsdna.devtools import gh_pr

    _gh_pr(monkeypatch, [CANCELLED_CHECK, FAILED_CHECK_RUN, COMPLETED_SUCCESS_CHECK_RUN])
    gh = GH({("POST", "repos/owner/repo/actions/runs/8/rerun"): None, ("POST", "repos/owner/repo/actions/runs/34882319228/rerun"): None})

    gh_pr.requeue(gh)  # type: ignore[arg-type]

    assert [c[1].split("/")[-2] for c in gh.calls] == ["8", "34882319228"]


def test_github_cancel_reason_only_for_a_run_a_comment_names() -> None:
    from bmsdna.devtools import gh_pr

    comment = {"body": pr_cancel.comment_body("deploy raced", [("CI", 8, "u")])}

    assert gh_pr.cancel_reason(GH({("GET", COMMENTS): [comment]}), 9, [CANCELLED_CHECK]) == "deploy raced"  # type: ignore[arg-type]
    assert gh_pr.cancel_reason(GH({("GET", COMMENTS): [comment]}), 9, [{**CANCELLED_CHECK, "detailsUrl": RUN_URL.format(77)}]) is None  # type: ignore[arg-type]
    assert gh_pr.cancel_reason(GH(), 9, [COMPLETED_SUCCESS_CHECK_RUN]) is None  # type: ignore[arg-type]  # no request at all


def test_github_info_reports_cancelled_only_with_a_matching_comment() -> None:
    pr = {"number": 9, "title": "t", "url": "https://github.com/owner/repo/pull/9", "state": "OPEN", "isDraft": False, "statusCheckRollup": [COMPLETED_SUCCESS_CHECK_RUN, CANCELLED_CHECK]}
    marked = GH({("GET", COMMENTS): [{"body": pr_cancel.comment_body("deploy raced", [("CI", 8, "u")])}]})

    plain = pr_info.github_info(pr, GitHubRemote("owner", "repo"))
    with_reason = pr_info.github_info(pr, GitHubRemote("owner", "repo"), marked)  # type: ignore[arg-type]
    unmarked = pr_info.github_info(pr, GitHubRemote("owner", "repo"), GH({("GET", COMMENTS): []}))  # type: ignore[arg-type]

    assert plain.build == unmarked.build == pr_info.BUILD_PASSING  # a superseded run stays harmless
    assert (with_reason.build, with_reason.cancel_reason) == (pr_info.BUILD_CANCELLED, "deploy raced")


# -- CLI ------------------------------------------------------------------------


def test_cli_cancel_build_needs_a_reason() -> None:
    assert runner.invoke(app, ["pr", "cancel-build"]).exit_code == 2


def test_cli_cancel_build_and_requeue_build_dispatch_on_azure_devops(monkeypatch) -> None:
    seen: list[tuple] = []
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.pr_build.cancel", lambda *a, **k: seen.append(("cancel", a, k)))
    monkeypatch.setattr("bmsdna.devtools.cli.pr_build.requeue", lambda *a, **k: seen.append(("requeue", a, k)))

    assert runner.invoke(app, ["pr", "cancel-build", "--reason", "wrong branch", "--pr-id", "5", "--pat", "p"]).exit_code == 0
    assert runner.invoke(app, ["pr", "requeue-build", "--all"]).exit_code == 0

    assert seen[0] == ("cancel", (REMOTE, "p", None, "wrong branch"), {"pr_id": 5})
    assert seen[1][0] == "requeue" and seen[1][1][3] is True


def test_a_stray_issue_number_in_a_note_is_not_a_build_id() -> None:
    body = pr_cancel.comment_body("deploy raced", [("CI", 100, "https://x/100")]) + "\n\nsee also #103 for the cause"

    assert pr_cancel.parse(body) == ("deploy raced", {100})
    assert pr_cancel.latest_reason([body], {103}) is None


def test_names_from_the_api_are_cleaned_before_they_are_printed(monkeypatch, capsys) -> None:
    evil = {**RUNNING, "definition": {"id": 3, "name": "Dep\x1b[2Jloy"}}
    _plumbing(monkeypatch, [evil])
    comments: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.pr_build.cancel_running_build", lambda *a: None)
    monkeypatch.setattr("bmsdna.devtools.pr_build.add_comment", lambda s, r, pr_id, content: comments.append(content))

    pr_build.cancel(REMOTE, "pat", None, "why", source_branch="feature-x")

    assert "\x1b" not in capsys.readouterr().out + comments[0]


def test_github_cancel_failing_to_comment_tells_how_to_post_it(monkeypatch) -> None:
    from bmsdna.devtools import gh_pr

    _gh_pr(monkeypatch, [RUNNING_CHECK])
    gh = GH({("POST", "repos/owner/repo/actions/runs/7/cancel"): None, ("POST", COMMENTS): SystemExit("HTTP 403")})

    with pytest.raises(SystemExit, match="bdt pr comment"):
        gh_pr.cancel(gh, "why")  # type: ignore[arg-type]
