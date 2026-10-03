"""`bdt pr info` (what the Claude Code mod polls) and `bdt issue take` (GitHub issue #68)."""

import json

import pytest
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
        ([PASS, SKIPPED], pr_info.BUILD_PASSING),
        ([PASS, RUNNING], pr_info.BUILD_PENDING),
        ([PASS, WAITING], pr_info.BUILD_WAITING),
        ([RUNNING, WAITING], pr_info.BUILD_WAITING),
        ([PASS, FAIL, RUNNING, WAITING], pr_info.BUILD_FAILING),
        ([PASS, CANCELLED], pr_info.BUILD_FAILING),
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
    "body": "Does the thing.\n\nFixes #68\n",
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


def test_github_info_without_issue_ref_has_no_issues() -> None:
    assert pr_info.github_info({**GH_PR, "body": None}, GITHUB_REMOTE).issues == []


def test_ado_info_reports_unknown_build_and_linked_work_item() -> None:
    pr = {
        "pullRequestId": 42,
        "title": "feat: widgets",
        "status": "active",
        "isDraft": False,
        "description": "see https://dev.azure.com/myorg/MyProj/_workitems/edit/7",
    }

    info = pr_info.ado_info(pr, ADO_REMOTE)

    assert info.build == pr_info.BUILD_UNKNOWN
    assert info.state == "open"
    assert info.url == "https://dev.azure.com/myorg/MyProj/_git/myrepo/pullrequest/42"
    assert [i.number for i in info.issues] == [7]
    assert info.issues[0].url == "https://dev.azure.com/myorg/MyProj/_workitems/edit/7"


def _github_cli(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.get_pr_info", lambda gh, pr_id=None: GH_PR)


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
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.get_pr_info", lambda gh, pr_id=None: seen.update(pr_id=pr_id) or GH_PR)

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
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.get_pr_info", lambda gh, pr_id=None: {**GH_PR, "body": "no refs"})

    result = runner.invoke(app, ["issue", "take"])

    assert result.exit_code != 0
    assert calls == []


def test_issue_take_with_several_closed_issues_is_ambiguous(monkeypatch) -> None:
    calls = _take_cli(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.get_pr_info", lambda gh, pr_id=None: {**GH_PR, "body": "Fixes #1\nFixes #2"})

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
