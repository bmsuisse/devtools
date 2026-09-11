from unittest.mock import MagicMock

import pytest

from bmsdna.devtools.gh_issue import (
    _extract_issue_numbers,
    _find_project_number,
    build_search_query,
    comment,
    create,
    parse_comment_id,
    parse_issue_number,
    resolve_board,
    search,
    update,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/owner/repo/issues/42", "42"),
        ("https://github.com/owner/repo/issues/42/", "42"),
        ("http://github.com/owner/repo/issues/1", "1"),
    ],
)
def test_parse_issue_number(url: str, expected: str) -> None:
    assert parse_issue_number(url) == expected


def test_parse_comment_id_present() -> None:
    url = "https://github.com/owner/repo/issues/42#issuecomment-123456789"
    assert parse_comment_id(url) == "123456789"


def test_parse_comment_id_absent() -> None:
    assert parse_comment_id("https://github.com/owner/repo/issues/42") is None


def test_build_search_query_keywords_only() -> None:
    assert build_search_query(["auth", "timeout"], None) == "auth timeout sort:updated-desc"


def test_build_search_query_adds_updated_qualifier() -> None:
    assert build_search_query(["auth"], "2026-08-10") == "auth sort:updated-desc updated:>=2026-08-10"


def test_build_search_query_no_keywords_still_scopes_by_date() -> None:
    assert build_search_query([], "2026-08-10") == "sort:updated-desc updated:>=2026-08-10"


def test_build_search_query_always_sorts_by_updated_desc() -> None:
    # search()'s "most recently updated first" ordering depends on this — GitHub's
    # default --search order is text relevance, not recency.
    assert "sort:updated-desc" in build_search_query(["auth"], None)


def test_search_defaults_to_open_state(monkeypatch) -> None:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    search("gh", "owner", [], None, 10)

    assert captured_cmd[captured_cmd.index("--state") + 1] == "open"


def test_search_passes_through_requested_state(monkeypatch) -> None:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    search("gh", "owner", ["auth"], None, 10, state="all")

    assert captured_cmd[captured_cmd.index("--state") + 1] == "all"


def test_search_board_overfetches_then_filters_to_limit(monkeypatch) -> None:
    import json

    captured_cmds: list[list[str]] = []

    issues = [{"number": i, "title": f"issue {i}", "url": f"https://github.com/owner/repo/issues/{i}", "state": "OPEN"} for i in range(1, 21)]
    # Board only has the even-numbered issues on it.
    board_items = [{"type": "Issue", "url": f"https://github.com/owner/repo/issues/{i}"} for i in range(1, 21) if i % 2 == 0]

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        if "list" in cmd and "issue" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")
        if "project" in cmd and "list" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps({"projects": [{"title": "Roadmap", "number": 7}]}), stderr="")
        if "item-list" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps({"items": board_items}), stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    results = search("gh", "owner", [], None, 3, board="Roadmap")

    issue_list_cmd = captured_cmds[0]
    assert issue_list_cmd[issue_list_cmd.index("--limit") + 1] == "30"  # limit * _BOARD_SEARCH_OVERFETCH
    assert len(results) == 3
    assert all(item["number"] % 2 == 0 for item in results)


def test_resolve_board_prefers_explicit_over_config(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.github]\nboard = "Config Board"\n')
    monkeypatch.chdir(tmp_path)
    assert resolve_board("Explicit Board") == "Explicit Board"


def test_resolve_board_falls_back_to_config(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.github]\nboard = "Config Board"\n')
    monkeypatch.chdir(tmp_path)
    assert resolve_board(None) == "Config Board"


def test_resolve_board_none_when_neither_given(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    assert resolve_board(None) is None


def test_find_project_number_matches_title_case_insensitively() -> None:
    projects = [{"title": "Backlog", "number": 3}, {"title": "Roadmap", "number": 7}]
    assert _find_project_number(projects, "roadmap") == 7


def test_find_project_number_none_when_not_found() -> None:
    assert _find_project_number([{"title": "Backlog", "number": 3}], "Roadmap") is None


def test_extract_issue_numbers_drops_pull_requests_and_draft_issues() -> None:
    items = [
        {"type": "Issue", "url": "https://github.com/owner/repo/issues/5"},
        {"type": "PullRequest", "url": "https://github.com/owner/repo/pull/6"},
        {"type": "DraftIssue", "title": "no url"},
    ]
    assert _extract_issue_numbers(items) == {5}


def _run_update_capturing_commands(monkeypatch, **update_kwargs) -> list[list[str]]:
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)
    update("gh", 42, **update_kwargs)
    return captured_cmds


@pytest.mark.parametrize("state", ["Closed", "Done", "completed"])
def test_update_closes_as_completed_for_done_states(monkeypatch, state: str) -> None:
    cmds = _run_update_capturing_commands(monkeypatch, state=state)
    assert cmds == [["gh", "issue", "close", "42", "--reason", "completed"]]


@pytest.mark.parametrize("state", ["Removed", "Not Planned", "wontfix"])
def test_update_closes_as_not_planned_for_removed_states(monkeypatch, state: str) -> None:
    cmds = _run_update_capturing_commands(monkeypatch, state=state)
    assert cmds == [["gh", "issue", "close", "42", "--reason", "not planned"]]


def test_update_reopens_for_open_state(monkeypatch) -> None:
    cmds = _run_update_capturing_commands(monkeypatch, state="Open")
    assert cmds == [["gh", "issue", "reopen", "42"]]


def test_update_comments_exact_state_when_unsupported(monkeypatch) -> None:
    cmds = _run_update_capturing_commands(monkeypatch, state="Active")
    assert cmds == [["gh", "issue", "comment", "42", "--body", "Requested state change to 'Active', which isn't a valid GitHub issue state — left unchanged."]]


def test_update_errors_with_nothing_to_do(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", MagicMock())
    with pytest.raises(SystemExit):
        update("gh", 42)


def _fake_push_assets(monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.gh_issue.push_assets",
        lambda owner, repo, key, paths, **kwargs: [f"https://github.com/{owner}/{repo}/blob/pr-assets/{key}/{i:02d}-{p.split('/')[-1]}?raw=true" for i, p in enumerate(paths)],
    )


def test_create_with_files_appends_attachments_section(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        if "create" in cmd:
            return MagicMock(returncode=0, stdout="https://github.com/owner/repo/issues/42\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    create("gh", "owner", "repo", "Bug title", "body text", [], [], [], file_paths=["/tmp/report.pdf"])

    edit_cmd = next(cmd for cmd in captured_cmds if "edit" in cmd)
    body = edit_cmd[edit_cmd.index("--body") + 1]
    assert "## Attachments" in body
    assert "[report.pdf]" in body
    assert "body text" in body


def test_create_with_board_passes_project_flag(monkeypatch) -> None:
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/issues/42\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    create("gh", "owner", "repo", "Bug title", "body text", [], [], [], board="Roadmap")

    create_cmd = captured_cmds[0]
    assert create_cmd[create_cmd.index("--project") + 1] == "Roadmap"


def test_create_without_board_omits_project_flag(monkeypatch) -> None:
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/issues/42\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    create("gh", "owner", "repo", "Bug title", "body text", [], [], [])

    assert "--project" not in captured_cmds[0]


def test_comment_with_screenshots_and_files_builds_both_sections(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/issues/42#issuecomment-1\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    comment("gh", "owner", "repo", 42, "Fixed", ["/tmp/shot.png"], ["/tmp/report.pdf"])

    body = captured_cmd[captured_cmd.index("--body") + 1]
    assert "Fixed" in body
    assert "## Screenshots" in body
    assert "## Attachments" in body
