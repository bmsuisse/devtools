"""`bdt pr info` (polled by status integrations) and `bdt issue take` (GitHub issue #68)."""

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
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.comment", lambda *args, **kw: calls.append(args))
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.last_comment", lambda *args: None)
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
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.last_comment", lambda *args: None)
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


def _target_options(command, path=()):
    # duck-typed: typer vendors its own click, so there's no `click` to isinstance against
    if hasattr(command, "commands"):
        for name, sub in command.commands.items():
            yield from _target_options(sub, (*path, name))
        return
    for param in command.params:
        if param.name in ("target", "target_branch"):
            yield " ".join(path), param.default


def test_every_target_option_defaults_to_dev() -> None:
    import typer.main

    found = dict(_target_options(typer.main.get_command(app)))

    assert {"pr create", "pr status", "pr info", "pr retry", "pr publish", "pr update", "pr comment", "pr watch-deploy", "issue take"} <= set(found)
    assert {k: v for k, v in found.items() if v != "dev"} == {}


# -- bdt issue do takes the issue first ---------------------------------------


def _do_cli(monkeypatch, *, agent_env: bool = False) -> dict:
    """GitHub issue 12 with `gh` stubbed; records the posted comments and the started agent command."""
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.cli._current_user", lambda remote: "octocat")
    monkeypatch.setattr("bmsdna.devtools.cli.issue_do_mod.fetch_github", lambda gh, n: ("add thing", "details"))
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.last_comment", lambda *args: None)
    seen: dict = {"events": [], "bodies": []}

    def run_gh(gh, args):
        seen["events"].append("comment")
        seen["bodies"].append(args[args.index("--body") + 1])
        return "https://x/issues/12#issuecomment-1"

    def start(cmd, check=False):
        seen["events"].append("agent")
        seen["cmd"] = cmd
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("bmsdna.devtools.gh_issue._run_gh", run_gh)
    monkeypatch.setattr("bmsdna.devtools.issue_do.shutil.which", lambda a: "/bin/" + a)
    monkeypatch.setattr("bmsdna.devtools.issue_do.subprocess.run", start)
    if agent_env:
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_OUTER")
    return seen


def test_issue_do_takes_the_issue_with_the_new_session_before_starting_the_agent(monkeypatch) -> None:
    seen = _do_cli(monkeypatch)

    result = runner.invoke(app, ["issue", "do", "12"])

    assert seen["events"] == ["comment", "agent"], result.output
    session_id = seen["cmd"][seen["cmd"].index("--session-id") + 1]
    assert seen["bodies"] == [f"Taken by octocat\n\nClaude Session: {session_id} (resume with `claude --resume {session_id}`)"]
    assert "bdt issue take" in seen["cmd"][2]  # the prompt says not to run it again


def test_issue_do_does_not_credit_the_outer_agent_session(monkeypatch) -> None:
    """`bdt issue do` can itself run inside an agent; that session must not end up on the comment."""
    seen = _do_cli(monkeypatch, agent_env=True)

    runner.invoke(app, ["issue", "do", "12"])

    assert "session_OUTER" not in seen["bodies"][0]


def test_issue_do_dry_run_posts_nothing(monkeypatch) -> None:
    seen = _do_cli(monkeypatch)

    result = runner.invoke(app, ["issue", "do", "12", "--dry-run"])

    assert result.exit_code == 0 and seen["events"] == []
    assert "--session-id" in result.output


def test_issue_do_other_agent_is_taken_without_a_session(monkeypatch) -> None:
    seen = _do_cli(monkeypatch)

    runner.invoke(app, ["issue", "do", "12", "--agent", "codex"])

    assert seen["bodies"] == ["Taken by octocat (via codex)"] and "--session-id" not in seen["cmd"]


# -- never take twice ---------------------------------------------------------


@pytest.mark.parametrize("last", ["Taken by octocat", "Taken by someone-else\n\nClaude Session: https://claude.ai/code/session_x", "  Taken by x"])
def test_issue_take_skips_when_the_last_comment_is_already_a_take(monkeypatch, last) -> None:
    calls = _take_cli(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.last_comment", lambda *args: last)

    result = runner.invoke(app, ["issue", "take", "12"])

    assert result.exit_code == 0 and calls == []
    assert "already taken" in result.output


def test_issue_take_comments_when_the_last_comment_is_something_else(monkeypatch) -> None:
    calls = _take_cli(monkeypatch)
    # an older take followed by a later discussion comment: the claim is no longer the latest word
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.last_comment", lambda *args: "Looks good, who is on this?")

    assert runner.invoke(app, ["issue", "take", "12"]).exit_code == 0
    assert len(calls) == 1


def test_issue_take_on_azure_devops_reads_the_html_comment(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.cli._current_user", lambda remote: "me")
    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.last_comment", lambda session, remote, n: "<div>Taken by <b>someone</b></div>")
    posted = []
    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.comment_with_screenshots", lambda *a, **k: posted.append(a))

    result = runner.invoke(app, ["issue", "take", "7"])

    assert result.exit_code == 0 and posted == [] and "already taken" in result.output


def test_issue_do_still_starts_the_agent_when_already_taken(monkeypatch) -> None:
    seen = _do_cli(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.last_comment", lambda *args: "Taken by octocat")

    runner.invoke(app, ["issue", "do", "12"])

    assert seen["events"] == ["agent"]


def test_gh_last_comment_reads_graphql(monkeypatch) -> None:
    from bmsdna.devtools import gh_issue

    out = json.dumps({"data": {"repository": {"issue": {"comments": {"nodes": [{"body": "Taken by x"}]}}}}})
    seen = {}
    monkeypatch.setattr(gh_issue, "_run_gh", lambda gh, args: seen.update(args=args) or out)

    assert gh_issue.last_comment("gh", "o", "r", 5) == "Taken by x"
    assert "number=5" in seen["args"] and "owner=o" in seen["args"]

    empty = json.dumps({"data": {"repository": {"issue": {"comments": {"nodes": []}}}}})
    monkeypatch.setattr(gh_issue, "_run_gh", lambda gh, args: empty)
    assert gh_issue.last_comment("gh", "o", "r", 5) is None


def test_ado_last_comment_asks_for_newest_first() -> None:
    from unittest.mock import MagicMock

    from bmsdna.devtools import ado_issue

    session = MagicMock()
    session.get.return_value.json.return_value = {"comments": [{"text": "<p>Taken by x</p>"}]}

    assert ado_issue.last_comment(session, ADO_REMOTE, 7) == "<p>Taken by x</p>"
    params = session.get.call_args.kwargs["params"]
    assert params["order"] == "desc" and params["$top"] == 1
