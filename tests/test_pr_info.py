"""`bdt pr info` (what the Claude Code mod polls) and `bdt issue take` (GitHub issue #68)."""

import json

import pytest
import requests
from typer.testing import CliRunner

from bmsdna.devtools import pr_info
from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote

runner = CliRunner()

GITHUB_REMOTE = GitHubRemote("owner", "repo")
ADO_REMOTE = AdoRemote("myorg", "MyProj", "myrepo")

PASS = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS", "name": "a"}
SKIPPED = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED", "name": "b"}
FAIL = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE", "name": "c"}
CANCELLED = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED", "name": "d"}
RUNNING = {"__typename": "CheckRun", "status": "IN_PROGRESS", "name": "e"}
WAITING = {"__typename": "CheckRun", "status": "WAITING", "name": "f"}


@pytest.mark.parametrize(
    "checks,expected",
    [
        ([], pr_info.BUILD_NONE),
        ([SKIPPED], pr_info.BUILD_NONE),
        ([PASS, SKIPPED], pr_info.BUILD_PASSING),
        ([PASS, RUNNING], pr_info.BUILD_PENDING),
        ([PASS, WAITING], pr_info.BUILD_WAITING),
        ([RUNNING, WAITING], pr_info.BUILD_WAITING),
        ([PASS, FAIL, RUNNING, WAITING], pr_info.BUILD_FAILING),
        # same as `bdt pr status`, which exits 0 for a PR whose only odd check was cancelled
        ([PASS, CANCELLED], pr_info.BUILD_PASSING),
    ],
)
def test_build_state_worst_state_wins(checks, expected) -> None:
    assert pr_info.build_state(checks) == expected


GH_PR = {
    "number": 69,
    "title": "feat: mod",
    "url": "https://github.com/owner/repo/pull/69",
    "state": "OPEN",
    "isDraft": True,
    "closingIssuesReferences": [{"number": 68, "url": "https://github.com/owner/repo/issues/68", "repository": {"name": "repo"}}],
    "statusCheckRollup": [PASS, RUNNING],
}


def test_github_info_links_pr_and_closed_issue() -> None:
    info = pr_info.github_info(GH_PR, GITHUB_REMOTE)

    assert info.to_json_dict() == {
        "number": 69,
        "title": "feat: mod",
        "url": "https://github.com/owner/repo/pull/69",
        "state": "open",
        "draft": True,
        "build": "pending",
        "issues": [{"number": 68, "url": "https://github.com/owner/repo/issues/68"}],
    }


def test_github_info_without_closing_issue_has_no_issues() -> None:
    assert pr_info.github_info({**GH_PR, "closingIssuesReferences": []}, GITHUB_REMOTE).issues == []


def test_github_info_ignores_closed_issues_of_other_repos() -> None:
    other = {"number": 9, "url": "https://github.com/owner/other/issues/9", "repository": {"name": "other"}}
    assert pr_info.github_info({**GH_PR, "closingIssuesReferences": [other]}, GITHUB_REMOTE).issues == []


def test_github_info_strips_terminal_escapes_from_title() -> None:
    info = pr_info.github_info({**GH_PR, "title": "evil\x1b]8;;http://x\x07 \u202etitle"}, GITHUB_REMOTE)
    assert info.title == "evil]8;;http://x title"


ADO_PR = {
    "pullRequestId": 42,
    "title": "feat: widgets",
    "status": "active",
    "isDraft": False,
    "sourceRefName": "refs/heads/feat/widgets",
    "description": "see https://dev.azure.com/myorg/MyProj/_workitems/edit/7",
}


def _build(status: str, result: str | None = None, pipeline: int = 1) -> dict:
    return {"id": 100 + pipeline, "definition": {"id": pipeline, "name": f"p{pipeline}"}, "status": status, "result": result}


@pytest.mark.parametrize(
    "builds,approval,expected",
    [
        ([], False, pr_info.BUILD_NONE),
        ([_build("completed", "succeeded"), _build("completed", "canceled", 2)], False, pr_info.BUILD_PASSING),
        ([_build("completed", "succeeded"), _build("inProgress", None, 2)], False, pr_info.BUILD_PENDING),
        ([_build("notStarted")], False, pr_info.BUILD_PENDING),
        ([_build("inProgress")], True, pr_info.BUILD_WAITING),
        ([_build("completed", "failed"), _build("inProgress", None, 2)], True, pr_info.BUILD_FAILING),
    ],
)
def test_ado_build_state_matches_pr_status(builds, approval, expected) -> None:
    assert pr_info.ado_build_state(builds, approval) == expected


def _ado_builds(monkeypatch, builds: list[dict], approvals: list | None = None) -> list[str]:
    """Stub the ADO build lookups; returns the source branches asked about."""
    asked: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.get_builds_for_pr", lambda s, r, branch, pr_id: asked.append(branch) or builds)
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.find_pending_approvals", lambda s, r, b: approvals or [])
    return asked


def test_ado_info_links_pr_and_work_item_and_judges_builds(monkeypatch) -> None:
    asked = _ado_builds(monkeypatch, [_build("completed", "succeeded"), _build("completed", "failed", 2)])

    info = pr_info.ado_info(None, ADO_PR, ADO_REMOTE)  # ty: ignore[invalid-argument-type]

    assert asked == ["feat/widgets"]
    assert info.build == pr_info.BUILD_FAILING
    assert info.state == "open"
    assert info.url == "https://dev.azure.com/myorg/MyProj/_git/myrepo/pullrequest/42"
    assert [i.number for i in info.issues] == [7]
    assert info.issues[0].url == "https://dev.azure.com/myorg/MyProj/_workitems/edit/7"


def test_ado_info_running_build_blocked_on_approval_is_waiting(monkeypatch) -> None:
    _ado_builds(monkeypatch, [_build("inProgress")], approvals=[("build", [], [])])

    assert pr_info.ado_info(None, ADO_PR, ADO_REMOTE).build == pr_info.BUILD_WAITING  # ty: ignore[invalid-argument-type]


def test_ado_info_unknown_build_when_ado_cannot_be_asked(monkeypatch) -> None:
    def boom(*a):
        raise requests.ConnectionError("down")

    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.get_builds_for_pr", boom)

    assert pr_info.ado_info(None, ADO_PR, ADO_REMOTE).build == pr_info.BUILD_UNKNOWN  # ty: ignore[invalid-argument-type]


def _github_cli(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.get_pr", lambda gh, pr_id=None, fields="": GH_PR)


def test_pr_info_json(monkeypatch) -> None:
    _github_cli(monkeypatch)

    result = runner.invoke(app, ["pr", "info", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["number"] == 69 and data["build"] == "pending" and data["issues"][0]["number"] == 68


def test_pr_info_text_mentions_pr_and_issue_links(monkeypatch) -> None:
    _github_cli(monkeypatch)

    result = runner.invoke(app, ["pr", "info"])

    assert result.exit_code == 0, result.output
    assert "https://github.com/owner/repo/pull/69" in result.output
    assert "https://github.com/owner/repo/issues/68" in result.output


def test_pr_info_passes_pr_id(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    seen = {}
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.get_pr", lambda gh, pr_id=None, fields="": seen.update(pr_id=pr_id) or GH_PR)

    assert runner.invoke(app, ["pr", "info", "--json", "--pr-id", "5"]).exit_code == 0
    assert seen == {"pr_id": 5}


# -- bdt issue take ---------------------------------------------------------


def _take_cli(monkeypatch, *, user: str = "octocat") -> list[tuple]:
    """GitHub remote with `gh api user` answering `user`; returns the recorded `gh_issue.comment` calls."""
    _github_cli(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli._current_user", lambda remote: user)
    calls: list[tuple] = []
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.comment", lambda *args: calls.append(args))
    return calls


def test_issue_take_with_number_comments_taken_by(monkeypatch) -> None:
    calls = _take_cli(monkeypatch)

    result = runner.invoke(app, ["issue", "take", "12"])

    assert result.exit_code == 0, result.output
    assert calls == [("gh", "owner", "repo", 12, "Taken by octocat", [])]


def test_issue_take_without_number_uses_issue_the_pr_closes(monkeypatch) -> None:
    calls = _take_cli(monkeypatch)

    result = runner.invoke(app, ["issue", "take"])

    assert result.exit_code == 0, result.output
    assert calls[0][3] == 68


def test_issue_take_without_number_and_without_pr_issue_fails(monkeypatch) -> None:
    calls = _take_cli(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.get_pr", lambda gh, pr_id=None, fields="": {**GH_PR, "closingIssuesReferences": []})

    result = runner.invoke(app, ["issue", "take"])

    assert result.exit_code != 0
    assert calls == []


def test_issue_take_with_several_closed_issues_is_ambiguous(monkeypatch) -> None:
    calls = _take_cli(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.get_pr", lambda gh, pr_id=None, fields="": {**GH_PR, "closingIssuesReferences": [{"number": 1, "url": "u1", "repository": {"name": "repo"}}, {"number": 2, "url": "u2", "repository": {"name": "repo"}}]})

    result = runner.invoke(app, ["issue", "take"])

    assert result.exit_code != 0
    assert calls == []


def test_issue_take_note_includes_session_link_under_claude(monkeypatch) -> None:
    """The session URL comes from `gh_issue.comment`'s own agent-session note (same as every other
    bdt comment), so `take` needs no session handling of its own -- verify that end to end."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc")
    _github_cli(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli._current_user", lambda remote: "octocat")
    bodies: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.gh_issue._run_gh", lambda gh, args: bodies.append(args[args.index("--body") + 1]) or "https://x/issues/12#issuecomment-1")

    result = runner.invoke(app, ["issue", "take", "12"])

    assert result.exit_code == 0, result.output
    assert bodies == ["Taken by octocat\n\nClaude Session: https://claude.ai/code/session_abc"]


def test_pr_info_on_azure_devops_passes_the_session_through(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli._resolve_ado_pr", lambda pat, remote, target, pr_id=None: ("session", ADO_PR))
    seen = {}
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.get_builds_for_pr", lambda s, r, branch, pr_id: seen.update(session=s) or [_build("completed", "succeeded")])

    result = runner.invoke(app, ["pr", "info", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["build"] == "passing" and seen == {"session": "session"}
