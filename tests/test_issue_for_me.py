"""`bdt issue search --for-me` and `bdt issue watch`: the backend pieces (comment-filtered search, comment
listing) and the CLI wiring. The mention matching and the watch loop themselves are in
`test_agent_mentions.py` / `test_issue_watch.py`."""

import json
import os
from unittest.mock import MagicMock

from typer.testing import CliRunner

from bmsdna.devtools import ado_issue, gh_issue
from bmsdna.devtools.agent_mentions import last_comment_filter
from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote
from bmsdna.devtools.issue_watch import Comment

runner = CliRunner()
REMOTE = AdoRemote("myorg", "MyProj", "myrepo")


# -- gh_issue.search(comment_filter=...) -------------------------------------------


def _gh_issues(n: int) -> list[dict]:
    return [{"number": i, "title": f"issue {i}", "url": f"https://github.com/o/r/issues/{i}", "state": "OPEN"} for i in range(1, n + 1)]


def _fake_gh(monkeypatch, issues: list[dict], last_comments: dict[int, str | None], graphql_calls: list[list[str]] | None = None, list_cmds: list[list[str]] | None = None):
    def fake_run(cmd, **kwargs):
        if cmd[1:3] == ["api", "graphql"]:
            number = int(cmd[cmd.index("-F") + 1].removeprefix("number="))
            if graphql_calls is not None:
                graphql_calls.append(cmd)
            body = last_comments.get(number)
            nodes = [{"body": body}] if body is not None else []
            return MagicMock(returncode=0, stdout=json.dumps({"data": {"repository": {"issue": {"comments": {"nodes": nodes}}}}}), stderr="")
        if list_cmds is not None:
            list_cmds.append(cmd)
        return MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)


def test_gh_search_keeps_only_issues_whose_last_comment_matches(monkeypatch, capsys) -> None:
    _fake_gh(monkeypatch, _gh_issues(4), {1: "@Claude please", 2: "thanks", 3: None, 4: "cc @Agent"})

    items = gh_issue.search("gh", "o", "r", [], None, 10, comment_filter=last_comment_filter("claude"))

    assert [i["number"] for i in items] == [1, 4]
    out = capsys.readouterr().out
    assert "#1 [OPEN] issue 1" in out and "#4 [OPEN] issue 4" in out
    assert "#2" not in out and "#3" not in out


def test_gh_search_overfetches_and_stops_checking_once_limit_matched(monkeypatch) -> None:
    list_cmds: list[list[str]] = []
    graphql_calls: list[list[str]] = []
    _fake_gh(monkeypatch, _gh_issues(20), {i: "@claude" for i in range(1, 21)}, graphql_calls, list_cmds)

    items = gh_issue.search("gh", "o", "r", [], None, 2, comment_filter=last_comment_filter("claude"))

    assert [i["number"] for i in items] == [1, 2]
    assert list_cmds[0][list_cmds[0].index("--limit") + 1] == "20"  # limit * overfetch
    assert len(graphql_calls) == 2


def test_gh_search_without_filter_makes_no_comment_calls(monkeypatch) -> None:
    graphql_calls: list[list[str]] = []
    _fake_gh(monkeypatch, _gh_issues(3), {}, graphql_calls)

    items = gh_issue.search("gh", "o", "r", [], None, 10)

    assert len(items) == 3
    assert graphql_calls == []


def test_gh_search_org_wide_looks_up_comments_in_each_items_own_repo(monkeypatch) -> None:
    items = [
        {"number": 5, "title": "x", "url": "u", "state": "open", "repository": {"nameWithOwner": "o/other"}},
        {"number": 6, "title": "y", "url": "u", "state": "open", "repository": {"nameWithOwner": "o/third"}},
    ]
    graphql_calls: list[list[str]] = []
    _fake_gh(monkeypatch, items, {5: "@claude", 6: "nope"}, graphql_calls)

    result = gh_issue.search("gh", "o", "", [], None, 10, org_wide=True, comment_filter=last_comment_filter("claude"))

    assert [i["number"] for i in result] == [5]
    assert any("repo=other" in arg for arg in graphql_calls[0])
    assert any("repo=third" in arg for arg in graphql_calls[1])


# -- gh_issue.list_comments ---------------------------------------------------------


def test_gh_list_comments_parses_paginated_stream(monkeypatch) -> None:
    captured: list[str] = []
    pretty = json.dumps({"id": 2, "author": "bob", "created_at": "t2", "url": "u2", "body": "second"}, indent=2)
    compact = json.dumps({"id": 1, "author": "alice", "created_at": "t1", "url": "u1", "body": "first"})

    def fake_run(cmd, **kwargs):
        captured[:] = cmd
        return MagicMock(returncode=0, stdout=f"{compact}\n{pretty}\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    comments = gh_issue.list_comments("gh", "o", "r", 84)

    assert [c["id"] for c in comments] == [1, 2]
    assert "--paginate" in captured
    assert "repos/o/r/issues/84/comments?per_page=100" in captured


def test_gh_list_comments_empty(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", lambda cmd, **kw: MagicMock(returncode=0, stdout="", stderr=""))
    assert gh_issue.list_comments("gh", "o", "r", 84) == []


# -- ado_issue ----------------------------------------------------------------------


def test_ado_list_comments_is_oldest_first_with_normalized_fields() -> None:
    session = MagicMock()
    session.get.return_value.json.return_value = {
        "comments": [
            {"id": 12, "text": "<div>newer</div>", "createdBy": {"displayName": "Bob"}, "createdDate": "2026-10-13T09:00:00Z"},
            {"id": 11, "text": "older", "createdBy": {"displayName": "Alice"}, "createdDate": "2026-10-13T08:00:00Z"},
        ]
    }

    comments = ado_issue.list_comments(session, REMOTE, 77)

    assert session.get.call_args.kwargs["params"]["order"] == "desc"
    assert [c["id"] for c in comments] == [11, 12]
    assert comments[1] == {
        "id": 12,
        "author": "Bob",
        "created_at": "2026-10-13T09:00:00Z",
        "url": "https://dev.azure.com/myorg/MyProj/_workitems/edit/77",
        "body": "<div>newer</div>",
    }


def test_ado_search_keeps_only_items_whose_last_comment_matches(capsys) -> None:
    session = MagicMock()
    session.post.return_value.json.return_value = {"workItems": [{"id": 1}, {"id": 2}, {"id": 3}]}
    work_items = {i: {"id": i, "fields": {"System.Title": f"item {i}", "System.State": "Active"}} for i in (1, 2, 3)}
    last = {1: "<div><a href=\"x\">@Claude</a> look</div>", 2: "<div>done</div>", 3: "@Agent"}

    def fake_get(url, params=None, **kwargs):
        resp = MagicMock()
        if url.endswith("/comments"):
            work_item_id = int(url.split("/workItems/")[1].split("/")[0])
            resp.json.return_value = {"comments": [{"id": 1, "text": last[work_item_id]}]}
        else:
            resp.json.return_value = {"value": [work_items[int(i)] for i in params["ids"].split(",")]}
        return resp

    session.get.side_effect = fake_get

    items = ado_issue.search(session, REMOTE, [], top=10, comment_filter=last_comment_filter("claude"))

    assert [i["id"] for i in items] == [1, 3]
    assert "item 2" not in capsys.readouterr().out
    assert session.post.call_args.kwargs["params"]["$top"] == 100  # overfetched


def test_ado_search_org_wide_checks_comments_in_the_items_own_project() -> None:
    session = MagicMock()
    session.post.return_value.json.return_value = {"workItems": [{"id": 1}]}
    urls: list[str] = []

    def fake_get(url, params=None, **kwargs):
        resp = MagicMock()
        if url.endswith("/comments"):
            urls.append(url)
            resp.json.return_value = {"comments": [{"id": 1, "text": "@claude"}]}
        else:
            resp.json.return_value = {"value": [{"id": 1, "fields": {"System.Title": "t", "System.State": "Active", "System.TeamProject": "Other"}}]}
        return resp

    session.get.side_effect = fake_get

    items = ado_issue.search(session, REMOTE, [], org_wide=True, comment_filter=last_comment_filter("claude"))

    assert len(items) == 1
    assert urls == ["https://dev.azure.com/myorg/Other/_apis/wit/workItems/1/comments"]


# -- CLI: issue search --for-me -----------------------------------------------------


def _clean_agent_env(monkeypatch) -> None:
    """The suite itself may run inside a coding agent, whose markers must not leak into detection."""
    for var in list(os.environ):
        if var in ("BDT_AGENT", "CLAUDECODE", "AI_AGENT", "GEMINI_CLI", "OPENCODE") or var.startswith(("CLAUDE_CODE_", "CODEX_", "GOOSE_")):
            monkeypatch.delenv(var)


def _capture_gh_search(monkeypatch) -> dict:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GitHubRemote("o", "r"))
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured: dict = {}

    def fake_search(gh, owner, repo, keywords, since, limit, state, board=None, org_wide=False, labels=None, comment_filter=None):
        captured["comment_filter"] = comment_filter
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.search", fake_search)
    return captured


def test_issue_search_for_me_uses_the_detected_agent(monkeypatch) -> None:
    _clean_agent_env(monkeypatch)
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    captured = _capture_gh_search(monkeypatch)

    result = runner.invoke(app, ["issue", "search", "--for-me"])

    assert result.exit_code == 0, result.output
    accept = captured["comment_filter"]
    assert accept("@Codex take this") and accept("@agent anyone?")
    assert not accept("@Claude take this")


def test_issue_search_agent_option_overrides_detection(monkeypatch) -> None:
    _clean_agent_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    captured = _capture_gh_search(monkeypatch)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--agent", "goose"])

    assert result.exit_code == 0, result.output
    assert captured["comment_filter"]("@Goose take this")
    assert not captured["comment_filter"]("@Claude take this")


def test_issue_search_bdt_agent_env_var_overrides_detection(monkeypatch) -> None:
    _clean_agent_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("BDT_AGENT", "codex")
    captured = _capture_gh_search(monkeypatch)

    result = runner.invoke(app, ["issue", "search", "--for-me"])

    assert result.exit_code == 0, result.output
    assert captured["comment_filter"]("@Codex take this")


def test_issue_search_for_me_without_an_agent_matches_only_agent(monkeypatch) -> None:
    _clean_agent_env(monkeypatch)
    captured = _capture_gh_search(monkeypatch)

    result = runner.invoke(app, ["issue", "search", "--for-me"])

    assert result.exit_code == 0, result.output
    assert "No calling agent detected" in result.output
    assert captured["comment_filter"]("@Agent hi")
    assert not captured["comment_filter"]("@Claude hi")


def test_issue_search_without_for_me_passes_no_filter(monkeypatch) -> None:
    _clean_agent_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    captured = _capture_gh_search(monkeypatch)

    result = runner.invoke(app, ["issue", "search"])

    assert result.exit_code == 0, result.output
    assert captured["comment_filter"] is None


def test_issue_search_for_me_on_azure_devops(monkeypatch) -> None:
    _clean_agent_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    captured: dict = {}

    def fake_search(session, remote, keywords, since=None, board=None, top=10, state="open", org_wide=False, tags=None, comment_filter=None):
        captured["comment_filter"] = comment_filter
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.search", fake_search)

    result = runner.invoke(app, ["issue", "search", "--for-me"])

    assert result.exit_code == 0, result.output
    assert captured["comment_filter"]("@Claude look")


# -- CLI: issue watch / watch-issue --------------------------------------------------


def _comment(id: int, body: str = "answer", author: str = "alice") -> Comment:
    return Comment(id, author, "2026-10-13T08:00:00Z", f"https://example.test/{id}", body)


def _patch_watch(monkeypatch, comments: list[Comment] | None, remote=None) -> dict:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote or GitHubRemote("o", "r"))
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured: dict = {}

    def fake_watch(fetch, *, since_id=None, accept=None, interval=20.0, timeout=None, **kwargs):
        captured.update(fetch=fetch, since_id=since_id, accept=accept, interval=interval, timeout=timeout)
        return comments

    monkeypatch.setattr("bmsdna.devtools.cli.issue_watch_mod.watch", fake_watch)
    return captured


def test_issue_watch_prints_new_comments_and_exits_zero(monkeypatch) -> None:
    captured = _patch_watch(monkeypatch, [_comment(5, "the answer")])

    result = runner.invoke(app, ["issue", "watch", "84", "--interval", "5", "--timeout", "60", "--since-comment", "4"])

    assert result.exit_code == 0, result.output
    assert "Comment #5 by alice" in result.output and "the answer" in result.output
    assert (captured["interval"], captured["timeout"], captured["since_id"]) == (5, 60, 4)


def test_issue_watch_json_output_is_one_object_per_line(monkeypatch) -> None:
    _patch_watch(monkeypatch, [_comment(5, "one"), _comment(6, "two")])

    result = runner.invoke(app, ["issue", "watch", "84", "--json"], catch_exceptions=False)

    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert [json.loads(line)["id"] for line in lines] == [5, 6]


def test_issue_watch_timeout_exits_4(monkeypatch) -> None:
    _patch_watch(monkeypatch, None)

    result = runner.invoke(app, ["issue", "watch", "84", "--timeout", "1"])

    assert result.exit_code == 4
    assert "No new comment on #84 within 1s" in result.output


def test_issue_watch_timeout_zero_means_no_timeout(monkeypatch) -> None:
    captured = _patch_watch(monkeypatch, [_comment(5)])

    result = runner.invoke(app, ["issue", "watch", "84", "--timeout", "0"])

    assert result.exit_code == 0, result.output
    assert captured["timeout"] is None


def test_watch_issue_alias(monkeypatch) -> None:
    _patch_watch(monkeypatch, [_comment(5)])
    assert runner.invoke(app, ["watch-issue", "84"]).exit_code == 0


def test_issue_watch_for_me_filters_to_mentions(monkeypatch) -> None:
    _clean_agent_env(monkeypatch)
    monkeypatch.setenv("AI_AGENT", "codex")
    captured = _patch_watch(monkeypatch, [_comment(5)])

    result = runner.invoke(app, ["issue", "watch", "84", "--for-me"])

    assert result.exit_code == 0, result.output
    assert captured["accept"](_comment(1, "@Codex ping"))
    assert not captured["accept"](_comment(2, "just chatting"))


def test_issue_watch_skip_own_uses_the_session_note(monkeypatch) -> None:
    _clean_agent_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.delenv("CLAUDE_CODE_BRIDGE_SESSION_ID", raising=False)
    captured = _patch_watch(monkeypatch, [_comment(5)])

    result = runner.invoke(app, ["issue", "watch", "84", "--skip-own"])

    assert result.exit_code == 0, result.output
    assert not captured["accept"](_comment(1, "question\n\nClaude Session: sess-1"))
    assert captured["accept"](_comment(2, "an answer"))


def test_issue_watch_skip_own_outside_an_agent_uses_the_current_user(monkeypatch) -> None:
    _clean_agent_env(monkeypatch)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_BRIDGE_SESSION_ID", raising=False)
    monkeypatch.setattr("bmsdna.devtools.cli._current_user", lambda remote: "alice")
    captured = _patch_watch(monkeypatch, [_comment(5)])

    result = runner.invoke(app, ["issue", "watch", "84", "--skip-own"])

    assert result.exit_code == 0, result.output
    assert not captured["accept"](_comment(1, author="Alice"))
    assert captured["accept"](_comment(2, author="bob"))


def test_issue_watch_github_fetch_lists_the_issues_comments(monkeypatch) -> None:
    captured = _patch_watch(monkeypatch, [_comment(5)])
    calls: list[tuple] = []
    monkeypatch.setattr(
        "bmsdna.devtools.cli.gh_issue.list_comments",
        lambda gh, owner, repo, number: calls.append((gh, owner, repo, number)) or [{"id": 9, "author": "a", "created_at": "t", "url": "u", "body": "b"}],
    )

    runner.invoke(app, ["issue", "watch", "84"])

    assert captured["fetch"]() == [Comment(9, "a", "t", "u", "b")]
    assert calls == [("gh", "o", "r", 84)]


def test_issue_watch_ado_fetch_converts_html_bodies(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    captured = _patch_watch(monkeypatch, [_comment(5)], remote=REMOTE)
    monkeypatch.setattr(
        "bmsdna.devtools.cli.ado_issue.list_comments",
        lambda session, remote, number: [{"id": 9, "author": "a", "created_at": "t", "url": "u", "body": "<div>hi &amp; bye</div>"}],
    )

    runner.invoke(app, ["issue", "watch", "84"])

    assert captured["fetch"]()[0].body == "hi & bye"


def test_issue_watch_rejects_a_non_positive_interval(monkeypatch) -> None:
    _patch_watch(monkeypatch, [_comment(5)])
    assert runner.invoke(app, ["issue", "watch", "84", "--interval", "0"]).exit_code == 2
