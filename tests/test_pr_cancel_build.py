"""`bdt pr cancel-build` / `requeue-build`, the `CANCELLED BUILD:` comment and its display in `pr info` / `pr status`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bmsdna.devtools import pr_build, pr_cancel, pr_info
from bmsdna.devtools.cli import app
from bmsdna.devtools.cli_tools import AgentSession
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

    session = pr_cancel.Session("Claude", "https://claude.ai/code/s")
    assert pr_cancel.parse(body + "\n\nClaude Session: https://claude.ai/code/s") == (pr_cancel.Reason("wrong branch", session), {100})


def test_parse_ignores_other_comments() -> None:
    assert pr_cancel.parse("LGTM, but CANCELLED BUILD: is not at the start") is None


def test_hand_written_comment_is_about_any_build() -> None:
    assert pr_cancel.parse("CANCELLED BUILD: infra down") == (pr_cancel.Reason("infra down"), set())


def test_reason_is_one_clean_line() -> None:
    parsed = pr_cancel.parse("CANCELLED BUILD: evil\x1b]8;;http://x\x07\n  text\n\nrest")

    assert parsed is not None and parsed[0].reason == "evil]8;;http://x text"


def test_latest_reason_is_the_newest_about_a_cancelled_build() -> None:
    comments = [
        pr_cancel.comment_body("first", [("CI", 100, "u")]),
        "unrelated",
        pr_cancel.comment_body("second", [("CI", 101, "u")]),
    ]

    assert pr_cancel.latest(comments, {100, 101}) == pr_cancel.Reason("second")
    assert pr_cancel.latest(comments, {100}) == pr_cancel.Reason("first")  # build 101 isn't the cancelled one any more
    assert pr_cancel.latest(comments, {999}) is None
    assert pr_cancel.latest(["unrelated"], {100}) is None


def test_notice_without_a_reason_says_so() -> None:
    assert "no 'CANCELLED BUILD:' comment" in pr_cancel.cancelled_notice(None)
    assert pr_cancel.cancelled_notice(pr_cancel.Reason("why")) == "Build cancelled: why"
    with_session = pr_cancel.cancelled_notice(pr_cancel.Reason("why", pr_cancel.Session("Codex", "abc")))
    assert with_session == "Build cancelled: why\nCancelled by Codex session: abc"


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


def test_cancellation_reads_the_pr_comments_for_the_cancelled_build() -> None:
    session = _session_with_comments([pr_cancel.comment_body("deploy raced", [("CI", 103, "u")], pr_cancel.Session("Codex", "abc"))])

    assert pr_build.cancellation(session, REMOTE, 42, [CANCELLED, FAILED]) == pr_cancel.Reason("deploy raced", pr_cancel.Session("Codex", "abc"))


def test_cancellation_is_none_without_a_cancelled_build_and_skips_the_request() -> None:
    session = MagicMock()

    assert pr_build.cancellation(session, REMOTE, 42, [FAILED]) is None
    session.get.assert_not_called()


def test_status_shows_why_the_build_was_cancelled_and_exits_1(monkeypatch, capsys) -> None:
    _plumbing(monkeypatch, [CANCELLED, SUCCEEDED])
    monkeypatch.setattr("bmsdna.devtools.pr_build.print_build", lambda *a: None)
    monkeypatch.setattr("bmsdna.devtools.pr_build.deploy_build_hint", lambda *a: None)
    monkeypatch.setattr("bmsdna.devtools.pr_build.cancellation", lambda *a: pr_cancel.Reason("deploy raced", pr_cancel.Session("Claude", "https://claude.ai/code/s")))

    with pytest.raises(SystemExit) as exc_info:
        pr_build.run(REMOTE, "pat", None, wait=False, source_branch="feature-x")

    out = capsys.readouterr().out
    assert exc_info.value.code == 1
    assert "Build cancelled: deploy raced" in out and "bdt pr requeue-build" in out
    assert "Cancelled by Claude session: https://claude.ai/code/s" in out


# -- pr info --------------------------------------------------------------------


def test_info_text_shows_the_cancel_reason() -> None:
    from bmsdna.devtools.cli import _info_text

    info = pr_info.PrInfo(1, "t", "https://x/1", "open", False, pr_info.BUILD_CANCELLED, cancel_reason="deploy raced", cancel_session=pr_cancel.Session("Codex", "abc"))

    assert "(open, build: cancelled)" in _info_text(info)
    assert "Build cancelled: deploy raced\nCancelled by Codex session: abc" in _info_text(info)
    assert info.to_json_dict()["cancel_reason"] == "deploy raced"
    assert info.to_json_dict()["cancel_session"] == {"agent": "Codex", "id": "abc"}


def test_ado_info_reports_cancelled_with_the_reason(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.get_builds_for_pr", lambda *a: [CANCELLED, SUCCEEDED])
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.find_pending_approvals", lambda *a: [])
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.cancellation", lambda *a: pr_cancel.Reason("deploy raced", pr_cancel.Session("Codex", "abc")))

    info = pr_info.ado_info(MagicMock(), PR, REMOTE)

    assert (info.build, info.cancel_reason, info.cancel_session) == (pr_info.BUILD_CANCELLED, "deploy raced", pr_cancel.Session("Codex", "abc"))


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


def test_github_cancellation_only_for_a_run_a_comment_names() -> None:
    from bmsdna.devtools import gh_pr

    comment = {"body": pr_cancel.comment_body("deploy raced", [("CI", 8, "u")])}

    assert gh_pr.cancellation(GH({("GET", COMMENTS): [comment]}), 9, [CANCELLED_CHECK]) == pr_cancel.Reason("deploy raced")  # type: ignore[arg-type]
    assert gh_pr.cancellation(GH({("GET", COMMENTS): [comment]}), 9, [{**CANCELLED_CHECK, "detailsUrl": RUN_URL.format(77)}]) is None  # type: ignore[arg-type]
    assert gh_pr.cancellation(GH(), 9, [COMPLETED_SUCCESS_CHECK_RUN]) is None  # type: ignore[arg-type]  # no request at all


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

    assert runner.invoke(app, ["pr", "cancel-build", "--reason", "wrong branch", "--pr-id", "5", "--pat", "p", "--session", "abc"]).exit_code == 0
    assert runner.invoke(app, ["pr", "requeue-build", "--all"]).exit_code == 0

    assert seen[0] == ("cancel", (REMOTE, "p", None, "wrong branch"), {"pr_id": 5, "session_id": "abc"})
    assert seen[1][0] == "requeue" and seen[1][1][3] is True


def test_a_stray_issue_number_in_a_note_is_not_a_build_id() -> None:
    body = pr_cancel.comment_body("deploy raced", [("CI", 100, "https://x/100")]) + "\n\nsee also #103 for the cause"

    assert pr_cancel.parse(body) == (pr_cancel.Reason("deploy raced"), {100})
    assert pr_cancel.latest([body], {103}) is None


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


# -- agent sessions --------------------------------------------------------------


def _detected(monkeypatch, agent: str | None, session_id: str = "sid") -> None:
    monkeypatch.setattr("bmsdna.devtools.pr_cancel.detect_agent_session", lambda: AgentSession(agent, session_id) if agent else None)


def test_resolve_session_defaults_to_the_detected_agent_whoever_it_is(monkeypatch) -> None:
    _detected(monkeypatch, None)
    assert pr_cancel.resolve_session(None) is None
    _detected(monkeypatch, "Codex", "019a")
    assert pr_cancel.resolve_session(None) == pr_cancel.Session("Codex", "019a")


def test_resolve_session_explicit_value_is_labelled_with_the_agent(monkeypatch) -> None:
    _detected(monkeypatch, "Codex")
    assert pr_cancel.resolve_session(" other-id ") == pr_cancel.Session("Codex", "other-id")
    assert pr_cancel.resolve_session("https://claude.ai/code/session_1") == pr_cancel.Session("Claude", "https://claude.ai/code/session_1")
    _detected(monkeypatch, None)
    assert pr_cancel.resolve_session("x") == pr_cancel.Session("Agent", "x")


@pytest.mark.parametrize("value", ["", "  ", "two words"])
def test_resolve_session_rejects_anything_but_one_id_or_url(monkeypatch, value) -> None:
    _detected(monkeypatch, None)
    with pytest.raises(ValueError):
        pr_cancel.resolve_session(value)


def test_cancel_records_the_session_of_a_codex_run(monkeypatch) -> None:
    _detected(monkeypatch, "Codex", "019a")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("AI_AGENT", "codex")
    monkeypatch.setenv("CODEX_THREAD_ID", "019a")
    _plumbing(monkeypatch, [RUNNING])
    comments: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.pr_build.cancel_running_build", lambda *a: None)
    monkeypatch.setattr("bmsdna.devtools.pr_build.add_comment", lambda s, r, pr_id, content: comments.append(content))

    pr_build.cancel(REMOTE, "pat", None, "why", source_branch="feature-x")

    assert comments[0].count("Codex Session: 019a") == 1  # not repeated by the footer every bdt comment gets
    assert pr_cancel.parse(comments[0]) == (pr_cancel.Reason("why", pr_cancel.Session("Codex", "019a")), {102})


def test_explicit_session_comes_first_and_wins(monkeypatch) -> None:
    _detected(monkeypatch, None)
    monkeypatch.setattr("bmsdna.devtools.cli_tools.detect_agent_session", lambda: AgentSession("Claude", "https://claude.ai/code/own"))
    _plumbing(monkeypatch, [RUNNING])
    comments: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.pr_build.cancel_running_build", lambda *a: None)
    monkeypatch.setattr("bmsdna.devtools.pr_build.add_comment", lambda s, r, pr_id, content: comments.append(content))

    pr_build.cancel(REMOTE, "pat", None, "why", source_branch="feature-x", session_id="https://claude.ai/code/other")

    parsed = pr_cancel.parse(comments[0])
    assert parsed is not None and parsed[0].session == pr_cancel.Session("Claude", "https://claude.ai/code/other")


# -- abandon --------------------------------------------------------------------


def test_abandon_comment_round_trips() -> None:
    body = pr_cancel.abandon_body("superseded by #99", pr_cancel.Session("Codex", "abc"))

    assert body == "ABANDONED PR: superseded by #99\n\nCodex Session: abc"
    assert pr_cancel.latest_abandonment(["x", body]) == pr_cancel.Reason("superseded by #99", pr_cancel.Session("Codex", "abc"))
    assert pr_cancel.latest_abandonment([pr_cancel.comment_body("a build", [("CI", 1, "u")])]) is None
    assert pr_cancel.latest_abandonment(["ABANDONED PR: first", "ABANDONED PR: second"]) == pr_cancel.Reason("second")
    assert "no 'ABANDONED PR:' comment" in pr_cancel.abandoned_notice(None)
    assert pr_cancel.abandoned_notice(pr_cancel.Reason("why", pr_cancel.Session("Claude", "s"))) == "PR abandoned: why\nAbandoned by Claude session: s"


def test_ado_abandon_abandons_then_posts_the_reason(monkeypatch, capsys) -> None:
    _detected(monkeypatch, None)
    _plumbing(monkeypatch, [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr_by_id", lambda *a: {**PR, "status": "active"})
    calls: list[tuple] = []
    monkeypatch.setattr("bmsdna.devtools.pr_build._patch_pr", lambda s, r, pr_id, fields: calls.append(("patch", pr_id, fields)) or {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.add_comment", lambda s, r, pr_id, content: calls.append(("comment", pr_id, content)))

    pr_build.abandon(REMOTE, "pat", None, "superseded", pr_id=42, session_id="abc")

    assert calls == [("patch", 42, {"status": "abandoned"}), ("comment", 42, "ABANDONED PR: superseded\n\nAgent Session: abc")]
    assert "Abandoned PR #42" in capsys.readouterr().out


def test_ado_abandon_needs_an_active_pr_and_a_reason(monkeypatch) -> None:
    _detected(monkeypatch, None)
    _plumbing(monkeypatch, [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr_by_id", lambda *a: {**PR, "status": "completed"})
    monkeypatch.setattr("bmsdna.devtools.pr_build._patch_pr", lambda *a: pytest.fail("not active"))

    with pytest.raises(SystemExit, match="already completed"):
        pr_build.abandon(REMOTE, "pat", None, "why", pr_id=42)
    with pytest.raises(SystemExit, match="reason is required"):
        pr_build.abandon(REMOTE, "pat", None, " ", pr_id=42)


def test_ado_abandon_failing_to_comment_says_how_to_post_it(monkeypatch) -> None:
    import requests

    _detected(monkeypatch, None)
    _plumbing(monkeypatch, [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr_by_id", lambda *a: {**PR, "status": "active"})
    monkeypatch.setattr("bmsdna.devtools.pr_build._patch_pr", lambda *a: {})

    def fail(*a):
        raise requests.ConnectionError("down")

    monkeypatch.setattr("bmsdna.devtools.pr_build.add_comment", fail)

    with pytest.raises(SystemExit, match="bdt pr comment"):
        pr_build.abandon(REMOTE, "pat", None, "why", pr_id=42)


def test_get_pr_finds_an_abandoned_pr_by_branch() -> None:
    session = MagicMock()
    asked: list[str] = []

    def get(url, params, timeout):
        asked.append(params["searchCriteria.status"])
        response = MagicMock()
        response.json.return_value = {"value": [{"pullRequestId": 7, "title": "t", "targetRefName": "refs/heads/dev"}] if params["searchCriteria.status"] == "abandoned" else []}
        return response

    session.get = get

    assert pr_build.get_pr(session, REMOTE, "feature-x", None)["pullRequestId"] == 7
    assert asked == ["active", "completed", "abandoned"]


def test_ado_info_of_an_abandoned_pr_says_why(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.get_builds_for_pr", lambda *a: [SUCCEEDED])
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.abandonment", lambda *a: pr_cancel.Reason("superseded", pr_cancel.Session("Codex", "abc")))

    info = pr_info.ado_info(MagicMock(), {**PR, "status": "abandoned"}, REMOTE)

    assert (info.state, info.abandon_reason, info.abandon_session) == ("closed", "superseded", pr_cancel.Session("Codex", "abc"))
    from bmsdna.devtools.cli import _info_text

    assert "PR abandoned: superseded\nAbandoned by Codex session: abc" in _info_text(info)


def test_github_abandon_closes_then_posts_the_reason(monkeypatch) -> None:
    from bmsdna.devtools import gh_pr

    _detected(monkeypatch, None)
    gh = GH({("GET", "repos/owner/repo/pulls/9"): {"state": "open"}, ("PATCH", "repos/owner/repo/pulls/9"): {}, ("POST", COMMENTS): {}})

    gh_pr.abandon(gh, "superseded", pr_id=9, session_id="abc")  # type: ignore[arg-type]

    assert [c[0] for c in gh.calls] == ["GET", "PATCH", "POST"]
    assert gh.calls[1][2] == {"state": "closed"}
    assert gh.calls[2][2] == {"body": "ABANDONED PR: superseded\n\nAgent Session: abc"}


def test_github_abandon_of_a_closed_pr_exits(monkeypatch) -> None:
    from bmsdna.devtools import gh_pr

    with pytest.raises(SystemExit, match="already closed"):
        gh_pr.abandon(GH({("GET", "repos/owner/repo/pulls/9"): {"state": "closed"}}), "why", pr_id=9)  # type: ignore[arg-type]


def test_github_info_of_a_closed_pr_says_why() -> None:
    pr = {"number": 9, "title": "t", "url": "https://github.com/owner/repo/pull/9", "state": "CLOSED", "isDraft": False, "statusCheckRollup": []}
    gh = GH({("GET", COMMENTS): [{"body": "ABANDONED PR: superseded"}]})

    info = pr_info.github_info(pr, GitHubRemote("owner", "repo"), gh)  # type: ignore[arg-type]

    assert (info.state, info.abandon_reason) == ("closed", "superseded")


# -- --wait ---------------------------------------------------------------------


class Slept(Exception):
    """Raised instead of sleeping: the command would have kept polling."""


def _no_sleep(monkeypatch) -> None:
    def sleep(self, seconds):
        raise Slept

    monkeypatch.setattr("bmsdna.devtools.pr_build.WaitDeadline.sleep", sleep)
    monkeypatch.setattr("bmsdna.devtools.pr_build.find_pending_approvals", lambda *a: [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.print_build", lambda *a: None)
    monkeypatch.setattr("bmsdna.devtools.pr_build.deploy_build_hint", lambda *a: None)
    monkeypatch.setattr("bmsdna.devtools.pr_build.cancellation", lambda *a: pr_cancel.Reason("deploy raced"))


def test_wait_reports_a_build_cancelled_before_it_started_instead_of_waiting_for_a_new_one(monkeypatch, capsys) -> None:
    _plumbing(monkeypatch, [{**CANCELLED, "sourceVersion": "abc"}])
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda *a, **k: {**PR, "lastMergeCommit": {"commitId": "abc"}})
    _no_sleep(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        pr_build.run(REMOTE, "pat", None, wait=True, source_branch="feature-x")

    assert exc_info.value.code == 1
    assert "Build cancelled: deploy raced" in capsys.readouterr().out


def test_wait_still_waits_when_the_cancelled_build_is_of_an_older_push(monkeypatch) -> None:
    _plumbing(monkeypatch, [{**CANCELLED, "sourceVersion": "old"}])
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda *a, **k: {**PR, "lastMergeCommit": {"commitId": "new"}})
    _no_sleep(monkeypatch)

    with pytest.raises(Slept):  # the build of the new push is about to appear
        pr_build.run(REMOTE, "pat", None, wait=True, source_branch="feature-x")


def test_wait_stops_when_the_build_gets_cancelled_while_waiting(monkeypatch, capsys) -> None:
    """The running build is not in the baseline; once it is cancelled the next poll reports it."""
    polls = iter([[RUNNING], [{**RUNNING, "status": "completed", "result": "canceled"}]])
    _plumbing(monkeypatch, [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_builds_for_pr", lambda *a, **k: next(polls))
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda *a, **k: {**PR, "lastMergeCommit": {"commitId": "abc"}})
    _no_sleep(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.pr_build.WaitDeadline.sleep", lambda self, seconds: None)  # one poll later, for real

    with pytest.raises(SystemExit) as exc_info:
        pr_build.run(REMOTE, "pat", None, wait=True, source_branch="feature-x")

    assert exc_info.value.code == 1 and "Build cancelled" in capsys.readouterr().out


def test_wait_stops_on_an_abandoned_pr_instead_of_polling_for_builds(monkeypatch, capsys) -> None:
    _plumbing(monkeypatch, [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda *a, **k: {**PR, "status": "abandoned"})
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_builds_for_pr", lambda *a, **k: pytest.fail("no builds are awaited for an abandoned PR"))
    monkeypatch.setattr("bmsdna.devtools.pr_build.abandonment", lambda *a: pr_cancel.Reason("superseded", pr_cancel.Session("Codex", "abc")))
    _no_sleep(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_builds_for_pr", lambda *a, **k: [])

    with pytest.raises(SystemExit) as exc_info:
        pr_build.run(REMOTE, "pat", None, wait=True, source_branch="feature-x")

    out = capsys.readouterr().out
    assert exc_info.value.code == 1
    assert "PR abandoned: superseded\nAbandoned by Codex session: abc" in out


def test_github_wait_stops_on_a_closed_pr(monkeypatch, capsys) -> None:
    from bmsdna.devtools import gh_pr

    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.get_pr",
        lambda gh, pr_id=None, **k: {"number": 9, "title": "t", "baseRefName": "main", "mergeable": "MERGEABLE", "state": "CLOSED", "statusCheckRollup": [RUNNING_CHECK]},
    )
    monkeypatch.setattr("bmsdna.devtools.gh_pr.time.sleep", lambda s: pytest.fail("polling on for a closed PR"))
    gh = GH({("GET", COMMENTS): [{"body": "ABANDONED PR: superseded"}]})

    with pytest.raises(SystemExit) as exc_info:
        gh_pr.run(gh, True, pr_id=9)  # type: ignore[arg-type]

    assert exc_info.value.code == 1 and "PR abandoned: superseded" in capsys.readouterr().out


def test_github_wait_with_a_cancelled_run_reports_and_exits_1(monkeypatch, capsys) -> None:
    from bmsdna.devtools import gh_pr

    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.get_pr",
        lambda gh, pr_id=None, **k: {"number": 9, "title": "t", "baseRefName": "main", "mergeable": "MERGEABLE", "state": "OPEN", "statusCheckRollup": [CANCELLED_CHECK]},
    )
    monkeypatch.setattr("bmsdna.devtools.gh_pr.deploy_run_hint", lambda *a: None)
    gh = GH({("GET", COMMENTS): [{"body": pr_cancel.comment_body("deploy raced", [("CI", 8, "u")])}]})

    with pytest.raises(SystemExit) as exc_info:
        gh_pr.run(gh, True, pr_id=9)  # type: ignore[arg-type]

    assert exc_info.value.code == 1 and "Build cancelled: deploy raced" in capsys.readouterr().out


# -- CLI --------------------------------------------------------------------------


def test_cli_abandon_needs_a_reason_and_a_single_session_value() -> None:
    assert runner.invoke(app, ["pr", "abandon"]).exit_code == 2
    assert runner.invoke(app, ["pr", "abandon", "--reason", "x", "--session", "two words"]).exit_code == 2
    assert runner.invoke(app, ["pr", "cancel-build", "--reason", "x", "--session", "two words"]).exit_code == 2


def test_cli_abandon_dispatches_on_azure_devops(monkeypatch) -> None:
    seen: list[tuple] = []
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.pr_build.abandon", lambda *a, **k: seen.append((a, k)))

    assert runner.invoke(app, ["pr", "abandon", "--reason", "superseded", "--pr-id", "5", "--pat", "p"]).exit_code == 0

    assert seen == [((REMOTE, "p", None, "superseded"), {"pr_id": 5, "session_id": None})]


# -- target branch without a build policy -----------------------------------------

PR_INTO_MAIN = {**PR, "targetRefName": "refs/heads/main"}


@pytest.mark.parametrize("command", ["cancel", "requeue", "retry"])
def test_no_builds_into_a_branch_without_policy_says_so(monkeypatch, command) -> None:
    _plumbing(monkeypatch, [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda *a, **k: PR_INTO_MAIN)
    monkeypatch.setattr("bmsdna.devtools.pr_build.lacks_build_policy", lambda s, r, branch: branch == "main")
    call = {
        "cancel": lambda: pr_build.cancel(REMOTE, "pat", None, "why", source_branch="feature-x"),
        "requeue": lambda: pr_build.requeue(REMOTE, "pat", None, source_branch="feature-x"),
        "retry": lambda: pr_build.retry(REMOTE, "pat", None, source_branch="feature-x"),
    }[command]

    with pytest.raises(SystemExit, match="target branch 'main' has no build policy"):
        call()


def test_no_builds_with_a_policy_does_not_blame_the_policy(monkeypatch) -> None:
    _plumbing(monkeypatch, [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda *a, **k: PR_INTO_MAIN)
    monkeypatch.setattr("bmsdna.devtools.pr_build.lacks_build_policy", lambda *a: False)

    with pytest.raises(SystemExit) as exc_info:
        pr_build.requeue(REMOTE, "pat", None, source_branch="feature-x")

    assert str(exc_info.value) == "No builds found for PR #42 -- nothing to requeue."


def test_wait_without_a_build_policy_gives_up_instead_of_polling_and_info_says_none(monkeypatch) -> None:
    _plumbing(monkeypatch, [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda *a, **k: PR_INTO_MAIN)
    monkeypatch.setattr("bmsdna.devtools.pr_build.lacks_build_policy", lambda *a: True)
    _no_sleep(monkeypatch)

    with pytest.raises(SystemExit, match="No build policy is configured on target branch 'main'"):
        pr_build.run(REMOTE, "pat", None, wait=True, source_branch="feature-x")

    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.get_builds_for_pr", lambda *a: [])
    assert pr_info.ado_info(MagicMock(), PR_INTO_MAIN, REMOTE).build == pr_info.BUILD_NONE
