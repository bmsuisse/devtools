"""`bdt issue search --for-me` and `bdt issue watch`: the backend pieces (comment-filtered search, comment
listing) and the CLI wiring. The mention matching and the watch loop themselves are in
`test_agent_mentions.py` / `test_issue_watch.py`."""

import json
import os
import subprocess
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from fake_github import FakeGitHub

from bmsdna.devtools import ado_issue, gh_issue, issue_watch as issue_watch_mod
from bmsdna.devtools.agent_mentions import last_comment_filter
from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote
from bmsdna.devtools.issue_watch import Comment

runner = CliRunner()
REMOTE = AdoRemote("myorg", "MyProj", "myrepo")


# -- gh_issue.search(comment_filter=...) -------------------------------------------


def _gh_issues(n: int) -> list[dict]:
    return [{"number": i, "title": f"issue {i}", "url": f"https://github.com/o/r/issues/{i}", "state": "OPEN"} for i in range(1, n + 1)]


def _fake_gh(monkeypatch, issues: list[dict], last_comments: dict[int, str | None], graphql_calls: list[dict] | None = None, list_cmds: list[dict] | None = None) -> FakeGitHub:
    def graphql(query: str, variables: dict) -> dict:
        if graphql_calls is not None:
            graphql_calls.append(variables)
        body = last_comments.get(variables["number"])
        nodes = [{"body": body}] if body is not None else []
        return {"repository": {"issue": {"comments": {"nodes": nodes}}}}

    def search(payload):
        if list_cmds is not None:
            list_cmds.append(payload)
        return [_hit(i) for i in issues]

    return FakeGitHub({"GET search/issues": search}, graphql=graphql)


def _hit(issue: dict) -> dict:
    """A `/search/issues` item for a `gh_issue.search`-shaped result dict."""
    repo = issue.get("repository", {}).get("nameWithOwner", "o/r")
    return {"number": issue["number"], "title": issue["title"], "html_url": issue["url"], "state": issue["state"], "repository_url": f"https://api.github.com/repos/{repo}"}


def test_gh_search_keeps_only_issues_whose_last_comment_matches(monkeypatch, capsys) -> None:
    gh = _fake_gh(monkeypatch, _gh_issues(4), {1: "@Claude please", 2: "thanks", 3: None, 4: "cc @Agent"})

    items = gh_issue.search(gh, "o", "r", [], None, 10, comment_filter=last_comment_filter("claude"))

    assert [i["number"] for i in items] == [1, 4]
    out = capsys.readouterr().out
    assert "#1 [OPEN] issue 1" in out and "#4 [OPEN] issue 4" in out
    assert "#2" not in out and "#3" not in out


def test_gh_search_overfetches_and_stops_checking_once_limit_matched(monkeypatch) -> None:
    list_cmds: list[dict] = []
    graphql_calls: list[dict] = []
    gh = _fake_gh(monkeypatch, _gh_issues(20), {i: "@claude" for i in range(1, 21)}, graphql_calls, list_cmds)

    items = gh_issue.search(gh, "o", "r", [], None, 2, comment_filter=last_comment_filter("claude"))

    assert [i["number"] for i in items] == [1, 2]
    assert list_cmds[0]["limit"] == 20  # limit * overfetch
    assert len(graphql_calls) == 2


def test_gh_search_without_filter_makes_no_comment_calls(monkeypatch) -> None:
    graphql_calls: list[dict] = []
    gh = _fake_gh(monkeypatch, _gh_issues(3), {}, graphql_calls)

    items = gh_issue.search(gh, "o", "r", [], None, 10)

    assert len(items) == 3
    assert graphql_calls == []


def test_gh_search_org_wide_looks_up_comments_in_each_items_own_repo(monkeypatch) -> None:
    items = [
        {"number": 5, "title": "x", "url": "u", "state": "open", "repository": {"nameWithOwner": "o/other"}},
        {"number": 6, "title": "y", "url": "u", "state": "open", "repository": {"nameWithOwner": "o/third"}},
    ]
    graphql_calls: list[dict] = []
    gh = _fake_gh(monkeypatch, items, {5: "@claude", 6: "nope"}, graphql_calls)

    result = gh_issue.search(gh, "o", "", [], None, 10, org_wide=True, comment_filter=last_comment_filter("claude"))

    assert [i["number"] for i in result] == [5]
    assert graphql_calls[0]["repo"] == "other" and graphql_calls[1]["repo"] == "third"


# -- gh_issue.list_comments ---------------------------------------------------------


def test_gh_list_comments_projects_and_orders_the_pages() -> None:
    raw = [
        {"id": 1, "user": {"login": "alice"}, "created_at": "t1", "html_url": "u1", "body": "first"},
        {"id": 2, "user": {"login": "bob"}, "created_at": "t2", "html_url": "u2", "body": "second"},
    ]
    gh = FakeGitHub({"GET repos/o/r/issues/84/comments": raw})

    comments = gh_issue.list_comments(gh, "o", "r", 84)

    assert [c["id"] for c in comments] == [1, 2]
    assert comments[1] == {"id": 2, "author": "bob", "created_at": "t2", "url": "u2", "body": "second"}


def test_gh_list_comments_empty() -> None:
    assert gh_issue.list_comments(FakeGitHub(), "o", "r", 84) == []


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


def test_gh_search_scan_limit_widens_the_scanned_pool_but_not_the_result(monkeypatch) -> None:
    list_cmds: list[dict] = []
    gh = _fake_gh(monkeypatch, _gh_issues(30), {i: "@Claude hi" for i in range(1, 31)}, list_cmds=list_cmds)

    items = gh_issue.search(gh, "o", "r", [], None, 1, comment_filter=lambda c: True, scan_limit=5)

    assert [i["number"] for i in items] == [1]
    assert list_cmds[0]["limit"] == 50


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


def _patch_take(monkeypatch, items: list[dict], remote=None, taken: list | None = None, search_limits: list | None = None) -> None:
    _clean_agent_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote or GitHubRemote("o", "r"))
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})

    def fake_search(*args, **kwargs):
        if search_limits is not None:
            search_limits.append((kwargs.get("top", args[5] if len(args) > 5 else None), kwargs.get("scan_limit")))
        return items

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.search", fake_search)
    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.search", fake_search)
    monkeypatch.setattr("bmsdna.devtools.cli._current_user", lambda remote: "claude-bot")
    monkeypatch.setattr("bmsdna.devtools.cli._take", lambda remote, number, message, pat, **kw: (taken.append((remote, number, message)) if taken is not None else None) or True)


def test_issue_search_take_claims_the_first_match(monkeypatch) -> None:
    taken: list = []
    limits: list = []
    _patch_take(monkeypatch, [{"number": 7}], taken=taken, search_limits=limits)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--limit", "5"])

    assert result.exit_code == 0, result.output
    assert taken == [(GitHubRemote("o", "r"), 7, "Taken by claude-bot")]
    assert limits == [(1, 5)]  # one result, out of a scan sized by --limit


def test_issue_search_take_on_azure_devops_uses_the_work_item_id(monkeypatch) -> None:
    taken: list = []
    _patch_take(monkeypatch, [{"id": 42}], remote=REMOTE, taken=taken)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take"])

    assert result.exit_code == 0, result.output
    assert [(r, n) for r, n, _ in taken] == [(REMOTE, 42)]


def test_issue_search_take_without_a_match_takes_nothing(monkeypatch) -> None:
    taken: list = []
    _patch_take(monkeypatch, [], taken=taken)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take"])

    assert result.exit_code == 0, result.output
    assert taken == []


def test_issue_search_take_org_wide_in_the_current_repo_needs_no_hint(monkeypatch) -> None:
    item = {"number": 7, "repository": {"nameWithOwner": "o/r"}}
    _patch_take(monkeypatch, [item])

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--org-wide"])

    assert result.exit_code == 0, result.output
    assert "find-repo" not in result.output


def test_issue_search_take_exits_1_when_someone_else_holds_the_claim(monkeypatch) -> None:
    _patch_take(monkeypatch, [{"number": 7}])
    monkeypatch.setattr("bmsdna.devtools.cli._take", lambda *a, **kw: False)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take"])

    assert result.exit_code == 1


def test_issue_search_take_requires_for_me(monkeypatch) -> None:
    taken: list = []
    _patch_take(monkeypatch, [{"number": 7}], taken=taken)

    result = runner.invoke(app, ["issue", "search", "--take"])

    assert result.exit_code == 2  # a usage error (the message itself is a rich box, so wrapped/coloured in CI)
    assert taken == []


def test_issue_search_take_org_wide_claims_the_match_in_its_own_repo(monkeypatch, tmp_path) -> None:
    taken: list = []
    item = {"number": 7, "repository": {"nameWithOwner": "otherorg/otherrepo"}}
    _patch_take(monkeypatch, [item], taken=taken)
    monkeypatch.setattr("bmsdna.devtools.cli.find_repo_mod.work_dir", lambda: tmp_path)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--org-wide"])

    assert result.exit_code == 0, result.output
    assert [(r, n) for r, n, _ in taken] == [(GitHubRemote("otherorg", "otherrepo"), 7)]
    assert "not cloned yet" in result.output and "bdt find-repo otherrepo" in result.output


def _git_clone_stub(path, origin: str) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)


def test_issue_search_take_org_wide_names_the_local_clone_when_there_is_one(monkeypatch, tmp_path) -> None:
    clone = tmp_path / "github" / "otherrepo"
    _git_clone_stub(clone, "https://github.com/otherorg/otherrepo.git")
    _git_clone_stub(tmp_path / "github" / "otherrepo-docs", "https://github.com/otherorg/otherrepo-docs.git")  # a substring match is not the repo
    item = {"number": 7, "repository": {"nameWithOwner": "otherorg/otherrepo"}}
    _patch_take(monkeypatch, [item])
    monkeypatch.setattr("bmsdna.devtools.cli.find_repo_mod.work_dir", lambda: tmp_path)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--org-wide"])

    assert result.exit_code == 0, result.output
    assert f"local clone: {clone}" in result.output
    assert "otherrepo-docs" not in result.output and "find-repo" not in result.output


def test_issue_search_take_org_wide_ignores_a_same_named_clone_of_another_owner(monkeypatch, tmp_path) -> None:
    _git_clone_stub(tmp_path / "github" / "otherrepo", "https://github.com/myorg/otherrepo.git")
    item = {"number": 7, "repository": {"nameWithOwner": "otherorg/otherrepo"}}
    _patch_take(monkeypatch, [item])
    monkeypatch.setattr("bmsdna.devtools.cli.find_repo_mod.work_dir", lambda: tmp_path)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--org-wide"])

    assert result.exit_code == 0, result.output
    assert "local clone" not in result.output and "bdt find-repo otherrepo" in result.output


def test_issue_search_take_rejects_a_limit_below_one(monkeypatch) -> None:
    taken: list = []
    _patch_take(monkeypatch, [{"number": 7}], taken=taken)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--limit", "0"])

    assert result.exit_code == 2
    assert taken == []


def test_issue_search_take_org_wide_on_azure_devops_claims_in_the_items_project(monkeypatch) -> None:
    taken: list = []
    item = {"id": 42, "fields": {"System.TeamProject": "OtherProj"}}
    _patch_take(monkeypatch, [item], remote=REMOTE, taken=taken)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--org-wide"])

    assert result.exit_code == 0, result.output
    assert [(r, n) for r, n, _ in taken] == [(AdoRemote("myorg", "OtherProj", "myrepo"), 42)]
    assert "OtherProj" in result.output and "bdt find-repo" in result.output


def test_gh_comment_names_its_repo_explicitly() -> None:
    gh = FakeGitHub({"POST repos/o/r/issues/7/comments": {"id": 1, "html_url": "https://github.com/o/r/issues/7#issuecomment-1"}})

    gh_issue.comment(gh, "o", "r", 7, "Taken by me", [], agent_note=False)

    assert gh.calls == [("POST", "repos/o/r/issues/7/comments", {"body": "Taken by me"})]


# -- CLI: issue search --for-me --wait ------------------------------------------------


class _WaitEnv:
    """`issue search --wait` against a fake backend: poll N finds `polls[N]` (the last one repeats), printing like the real
    backend does, and time only moves when the command sleeps -- so a wait runs instantly and deterministically."""

    def __init__(self, monkeypatch, polls: list, remote=None) -> None:
        self.taken: list = []
        self.search_calls = 0
        self.now = 1000.0
        self.sleeps: list[float] = []
        _patch_take(monkeypatch, [], remote=remote, taken=self.taken)
        is_ado = remote is not None

        def fake_search(*args, **kwargs):
            poll = polls[min(self.search_calls, len(polls) - 1)]
            self.search_calls += 1
            if isinstance(poll, BaseException):
                raise poll
            for item in poll:
                print(f"#{item['id' if is_ado else 'number']} [OPEN] some issue")
            if not poll:
                print("No matching issues found.")
            return poll

        monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.search", fake_search)
        monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.search", fake_search)

        real_poll_until = issue_watch_mod.poll_until

        def sleep(secs: float) -> None:
            self.sleeps.append(secs)
            self.now += secs

        monkeypatch.setattr(
            "bmsdna.devtools.cli.issue_watch_mod.poll_until",
            lambda probe, **kw: real_poll_until(probe, **kw, sleep=sleep, clock=lambda: self.now, log=lambda msg: None),
        )


def test_issue_search_wait_returns_a_match_on_the_first_poll_without_sleeping(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[{"number": 7}]])

    result = runner.invoke(app, ["issue", "search", "--for-me", "--wait"])

    assert result.exit_code == 0, result.output
    assert env.search_calls == 1 and env.sleeps == []
    assert "#7 [OPEN] some issue" in result.stdout


def test_issue_search_wait_polls_until_something_matches(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[], [], [{"number": 7}]])

    result = runner.invoke(app, ["issue", "search", "--for-me", "--wait", "--interval", "5"])

    assert result.exit_code == 0, result.output
    assert env.search_calls == 3 and env.sleeps == [5, 5]
    # what the empty polls printed is not part of the result
    assert result.stdout == "#7 [OPEN] some issue\n"


def test_issue_search_wait_output_equals_a_plain_search(monkeypatch) -> None:
    _WaitEnv(monkeypatch, [[], [{"number": 7}, {"number": 8}]])
    waited = runner.invoke(app, ["issue", "search", "--for-me", "--wait"])
    _WaitEnv(monkeypatch, [[{"number": 7}, {"number": 8}]])
    plain = runner.invoke(app, ["issue", "search", "--for-me"])

    assert waited.exit_code == plain.exit_code == 0
    assert waited.stdout == plain.stdout == "#7 [OPEN] some issue\n#8 [OPEN] some issue\n"


def test_issue_search_wait_times_out_with_exit_4_and_claims_nothing(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[]])

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--wait", "--timeout", "50", "--interval", "20"])

    assert result.exit_code == 4
    assert env.sleeps == [20, 20, 10]  # the last sleep is cut to the deadline
    assert env.taken == []
    assert "No matching" not in result.stdout and result.stdout == ""
    assert "turned up within 50 s" in result.stderr


def test_issue_search_wait_announces_itself_on_stderr_only(monkeypatch) -> None:
    _WaitEnv(monkeypatch, [[], [{"number": 7}]])

    result = runner.invoke(app, ["issue", "search", "--for-me", "--wait", "--timeout", "100", "--agent", "claude"])

    assert "Waiting up to 100 s" in result.stderr and "@claude" in result.stderr
    assert "Waiting" not in result.stdout


def test_issue_search_wait_defaults_match_issue_watch(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[]])

    result = runner.invoke(app, ["issue", "search", "--for-me", "--wait"])

    assert result.exit_code == 4
    assert env.sleeps == [issue_watch_mod.DEFAULT_INTERVAL] * 45  # 900 s / 20 s
    assert (issue_watch_mod.DEFAULT_INTERVAL, issue_watch_mod.DEFAULT_TIMEOUT) == (20.0, 900.0)


def test_issue_search_wait_timeout_zero_waits_until_a_match(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[]] * 100 + [[{"number": 7}]])

    result = runner.invoke(app, ["issue", "search", "--for-me", "--wait", "--timeout", "0"])

    assert result.exit_code == 0, result.output
    assert env.search_calls == 101


def test_issue_search_wait_take_claims_exactly_once_and_only_after_a_match(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[], [], [{"number": 7}]])
    claims_at_search: list[int] = []
    real_take = env.taken.append

    monkeypatch.setattr(
        "bmsdna.devtools.cli._take",
        lambda remote, number, message, pat, **kw: (claims_at_search.append(env.search_calls), real_take((remote, number, message))) and True,
    )

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--wait"])

    assert result.exit_code == 0, result.output
    assert env.taken == [(GitHubRemote("o", "r"), 7, "Taken by claude-bot")]
    assert claims_at_search == [3]  # nothing was claimed while polling
    assert result.stdout == "#7 [OPEN] some issue\n"


def test_issue_search_wait_take_exits_1_when_someone_else_holds_the_claim(monkeypatch) -> None:
    _WaitEnv(monkeypatch, [[{"number": 7}]])
    monkeypatch.setattr("bmsdna.devtools.cli._take", lambda *a, **kw: False)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--wait"])

    assert result.exit_code == 1


def test_issue_search_wait_take_org_wide_still_hints_at_the_other_repo(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[], [{"number": 7, "repository": {"nameWithOwner": "o/other"}}]])
    monkeypatch.setattr("bmsdna.devtools.cli._find_repo_hint", lambda target: f"HINT {target.owner}/{target.repo}")

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--wait", "--org-wide"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "HINT o/other"
    assert [(r, n) for r, n, _ in env.taken] == [(GitHubRemote("o", "other"), 7)]


def test_issue_search_wait_on_azure_devops(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[], [{"id": 42}]], remote=REMOTE)

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--wait"])

    assert result.exit_code == 0, result.output
    assert [(r, n) for r, n, _ in env.taken] == [(REMOTE, 42)]


def test_issue_search_wait_requires_for_me(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[{"number": 7}]])

    result = runner.invoke(app, ["issue", "search", "--wait"])

    assert result.exit_code == 2
    assert env.search_calls == 0


def test_issue_search_timeout_and_interval_need_wait(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[{"number": 7}]])

    for option in ("--timeout", "--interval"):
        result = runner.invoke(app, ["issue", "search", "--for-me", option, "5"])
        assert result.exit_code == 2, option
    assert env.search_calls == 0


def test_issue_search_wait_rejects_invalid_interval_and_timeout(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[{"number": 7}]])

    for args in (["--interval", "0"], ["--interval", "-1"], ["--timeout", "-1"]):
        result = runner.invoke(app, ["issue", "search", "--for-me", "--wait", *args])
        assert result.exit_code == 2, args
    assert env.search_calls == 0


def test_issue_search_wait_a_failing_poll_is_not_a_success(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [SystemExit("gh: network down")])

    result = runner.invoke(app, ["issue", "search", "--for-me", "--take", "--wait"])

    assert result.exit_code not in (0, 4)
    assert env.search_calls == issue_watch_mod.MAX_CONSECUTIVE_ERRORS
    assert env.taken == []


def test_issue_search_wait_retries_a_single_failed_poll(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [RuntimeError("blip"), [{"number": 7}]])

    result = runner.invoke(app, ["issue", "search", "--for-me", "--wait"])

    assert result.exit_code == 0, result.output
    assert env.search_calls == 2


def test_issue_search_without_wait_keeps_printing_no_match_and_exits_0(monkeypatch) -> None:
    env = _WaitEnv(monkeypatch, [[]])

    result = runner.invoke(app, ["issue", "search", "--for-me"])

    assert result.exit_code == 0
    assert result.stdout == "No matching issues found.\n"
    assert env.search_calls == 1 and env.sleeps == []


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


@pytest.mark.parametrize("option", ["--timeout", "--interval"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_issue_search_wait_rejects_non_finite_numbers(monkeypatch, option: str, value: str) -> None:
    _patch_take(monkeypatch, [{"number": 7}])

    result = runner.invoke(app, ["issue", "search", "--for-me", "--wait", option, value])

    assert result.exit_code == 2


@pytest.mark.parametrize("option", ["--timeout", "--interval"])
def test_issue_watch_rejects_non_finite_numbers(option: str) -> None:
    assert runner.invoke(app, ["issue", "watch", "84", option, "nan"]).exit_code == 2
