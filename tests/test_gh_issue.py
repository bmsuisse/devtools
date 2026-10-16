from unittest.mock import MagicMock

import pytest

from bmsdna.devtools.gh_issue import (
    _extract_issue_numbers,
    _find_project_number,
    add_pr_available_label,
    build_search_query,
    comment,
    create,
    ensure_pr_available_label,
    get_details,
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

    search("gh", "owner", "repo", [], None, 10)

    assert captured_cmd[captured_cmd.index("--state") + 1] == "open"


def test_search_passes_through_requested_state(monkeypatch) -> None:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    search("gh", "owner", "repo", ["auth"], None, 10, state="all")

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

    results = search("gh", "owner", "repo", [], None, 3, board="Roadmap")

    issue_list_cmd = captured_cmds[0]
    assert issue_list_cmd[issue_list_cmd.index("--limit") + 1] == "30"  # limit * _BOARD_SEARCH_OVERFETCH
    assert len(results) == 3
    assert all(item["number"] % 2 == 0 for item in results)


def test_search_org_wide_uses_gh_search_issues_with_owner(monkeypatch) -> None:
    import json

    captured_cmd: list[str] = []
    items = [
        {"number": 5, "title": "org-wide bug", "url": "https://github.com/owner/other-repo/issues/5", "state": "open", "repository": {"nameWithOwner": "owner/other-repo"}}
    ]

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps(items), stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    results = search("gh", "owner", "repo", ["auth"], None, 10, org_wide=True)

    assert captured_cmd[1:3] == ["search", "issues"]
    assert captured_cmd[captured_cmd.index("--owner") + 1] == "owner"
    assert "--repo" not in captured_cmd
    assert results == items


def test_search_org_wide_all_state_omits_state_flag(monkeypatch) -> None:
    import json

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps([]), stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    search("gh", "owner", "repo", [], None, 10, state="all", org_wide=True)

    # `gh search issues --state` (unlike `gh issue list --state`) has no 'all' value -- it must
    # be omitted, not forwarded, or `gh` rejects it outright.
    assert "--state" not in captured_cmd


def test_search_org_wide_passes_labels_as_repeatable_flag(monkeypatch) -> None:
    import json

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps([]), stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    search("gh", "owner", "repo", [], None, 10, org_wide=True, labels=["bug", "urgent"])

    label_indices = [i for i, arg in enumerate(captured_cmd) if arg == "--label"]
    assert [captured_cmd[i + 1] for i in label_indices] == ["bug", "urgent"]


def test_search_non_org_wide_passes_labels_as_repeatable_flag(monkeypatch) -> None:
    import json

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps([]), stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    search("gh", "owner", "repo", [], None, 10, labels=["bug"])

    assert captured_cmd[captured_cmd.index("--label") + 1] == "bug"


def test_search_org_wide_and_board_together_exits(monkeypatch) -> None:
    with pytest.raises(SystemExit):
        search("gh", "owner", "repo", [], None, 10, board="Roadmap", org_wide=True)


def test_search_board_ignores_same_number_issue_from_a_different_repo(monkeypatch) -> None:
    import json

    # This repo's own #7, plus a same-numbered #7 from an unrelated repo that also happens to
    # be on the (org-wide) board -- only the former should count as a match.
    issues = [{"number": 7, "title": "our issue", "url": "https://github.com/owner/repo/issues/7", "state": "OPEN"}]
    board_items = [{"type": "Issue", "url": "https://github.com/owner/other-repo/issues/7"}]

    def fake_run(cmd, **kwargs):
        if "list" in cmd and "issue" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")
        if "item-list" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps({"items": board_items}), stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    results = search("gh", "owner", "repo", [], None, 10, board="7")

    assert results == []


def test_resolve_project_number_list_call_includes_a_limit(monkeypatch) -> None:
    import json

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps({"projects": [{"title": "Roadmap", "number": 7}]}), stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    from bmsdna.devtools.gh_issue import resolve_project_number

    assert resolve_project_number("gh", "owner", "Roadmap") == 7
    assert "--limit" in captured_cmd


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
    assert _extract_issue_numbers(items, "owner", "repo") == {5}


def test_extract_issue_numbers_drops_issues_from_other_repos() -> None:
    items = [
        {"type": "Issue", "url": "https://github.com/owner/repo/issues/5"},
        {"type": "Issue", "url": "https://github.com/owner/other-repo/issues/5"},
        {"type": "Issue", "url": "https://github.com/other-owner/repo/issues/5"},
    ]
    assert _extract_issue_numbers(items, "owner", "repo") == {5}


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


def test_create_appends_agent_session_note_when_detected(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/issues/42\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    create("gh", "owner", "repo", "Bug title", "body text", [], [], [])

    body = captured_cmd[captured_cmd.index("--body") + 1]
    assert body == "body text\n\nClaude Session: https://claude.ai/code/session_abc123"


def test_create_without_agent_leaves_body_untouched(monkeypatch) -> None:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/issues/42\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    create("gh", "owner", "repo", "Bug title", "body text", [], [], [])

    assert captured_cmd[captured_cmd.index("--body") + 1] == "body text"


def test_comment_appends_agent_session_note_when_detected(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/issues/42#issuecomment-1\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    comment("gh", "owner", "repo", 42, "Fixed", [], [])

    body = captured_cmd[captured_cmd.index("--body") + 1]
    assert body == "Fixed\n\nClaude Session: https://claude.ai/code/session_abc123"


# -- ensure_pr_available_label / add_pr_available_label ----------------------


def test_ensure_pr_available_label_creates_label_when_missing(monkeypatch) -> None:
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        if cmd[1:3] == ["label", "list"]:
            return MagicMock(returncode=0, stdout="[]", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    ensure_pr_available_label("gh")

    create_cmd = next(cmd for cmd in captured_cmds if cmd[1:3] == ["label", "create"])
    assert create_cmd[3] == "pr-available"


def test_ensure_pr_available_label_skips_create_when_label_already_exists(monkeypatch) -> None:
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        if cmd[1:3] == ["label", "list"]:
            return MagicMock(returncode=0, stdout='[{"name": "pr-available"}]', stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    ensure_pr_available_label("gh")

    assert not any(cmd[1:3] == ["label", "create"] for cmd in captured_cmds)


def test_add_pr_available_label_adds_the_label_to_the_issue(monkeypatch) -> None:
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    add_pr_available_label("gh", 42)

    # Doesn't itself check/create the label -- that's `ensure_pr_available_label`'s job, called
    # once per `pr create` invocation rather than once per issue (see cli.py).
    assert not any(cmd[1:3] == ["label", "list"] for cmd in captured_cmds)
    assert not any(cmd[1:3] == ["label", "create"] for cmd in captured_cmds)
    edit_cmd = next(cmd for cmd in captured_cmds if cmd[1:3] == ["issue", "edit"])
    assert edit_cmd[3] == "42"
    assert edit_cmd[edit_cmd.index("--add-label") + 1] == "pr-available"


def _fake_gh(monkeypatch, responses: dict[str, str] | None = None) -> list[list[str]]:
    """Capture every `gh` call; `responses` maps a subcommand ("issue view", "api", ...) to its stdout."""
    cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        cmds.append(cmd)
        return MagicMock(returncode=0, stdout=(responses or {}).get(" ".join(cmd[1:3]), ""), stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)
    return cmds


def test_create_with_parent_and_blockers_passes_the_gh_flags(monkeypatch) -> None:
    cmds = _fake_gh(monkeypatch, {"issue create": "https://github.com/owner/repo/issues/42\n"})

    create("gh", "owner", "repo", "Title", "body", [], [], [], parent=5, blocked_by=[7, 8, 7])

    create_cmd = cmds[0]
    assert create_cmd[create_cmd.index("--parent") + 1] == "5"
    assert [create_cmd[i + 1] for i, arg in enumerate(create_cmd) if arg == "--blocked-by"] == ["7", "8"]


def test_create_without_links_omits_the_link_flags(monkeypatch) -> None:
    cmds = _fake_gh(monkeypatch, {"issue create": "https://github.com/owner/repo/issues/42\n"})

    create("gh", "owner", "repo", "Title", "body", [], [], [])

    assert "--parent" not in cmds[0]
    assert "--blocked-by" not in cmds[0]


_NO_LINKS = '{"parent": null, "blockedBy": {"nodes": [], "totalCount": 0}}'


def test_update_links_parent_and_blockers_that_are_missing(monkeypatch) -> None:
    cmds = _fake_gh(monkeypatch, {"issue view": _NO_LINKS})

    update("gh", 42, parent=5, blocked_by=[7, 8])

    assert cmds[0] == ["gh", "issue", "view", "42", "--json", "parent,blockedBy"]
    assert cmds[1] == ["gh", "issue", "edit", "42", "--parent", "5", "--add-blocked-by", "7", "--add-blocked-by", "8"]
    assert len(cmds) == 2


def test_update_skips_links_that_already_exist(monkeypatch, capsys) -> None:
    current = '{"parent": {"number": 5}, "blockedBy": {"nodes": [{"number": 7}], "totalCount": 1}}'
    cmds = _fake_gh(monkeypatch, {"issue view": current})

    update("gh", 42, parent=5, blocked_by=[7])

    assert [c[1:3] for c in cmds] == [["issue", "view"]]  # no edit at all
    assert "nothing to do" in capsys.readouterr().out


def test_update_only_adds_the_blockers_that_are_missing_and_replaces_a_different_parent(monkeypatch) -> None:
    current = '{"parent": {"number": 4}, "blockedBy": {"nodes": [{"number": 7}], "totalCount": 1}}'
    cmds = _fake_gh(monkeypatch, {"issue view": current})

    update("gh", 42, parent=5, blocked_by=[7, 8])

    assert cmds[1] == ["gh", "issue", "edit", "42", "--parent", "5", "--add-blocked-by", "8"]


def test_update_links_alongside_other_fields_in_one_edit(monkeypatch) -> None:
    cmds = _fake_gh(monkeypatch, {"issue view": _NO_LINKS})

    update("gh", 42, title="New", blocked_by=[7])

    assert cmds[1] == ["gh", "issue", "edit", "42", "--title", "New", "--add-blocked-by", "7"]


def test_update_rejects_linking_an_issue_to_itself(monkeypatch) -> None:
    cmds = _fake_gh(monkeypatch)

    with pytest.raises(SystemExit):
        update("gh", 42, parent=42)
    assert cmds == []


def test_get_details_maps_gh_json_and_comments(monkeypatch) -> None:
    view = (
        '{"number": 42, "title": "Widget", "state": "OPEN", "url": "https://github.com/owner/repo/issues/42", "body": "It breaks",'
        ' "labels": [{"name": "bug"}], "assignees": [{"login": "jane"}], "milestone": {"title": "v1"},'
        ' "parent": {"number": 5}, "subIssues": {"nodes": [{"number": 9}], "totalCount": 1},'
        ' "blockedBy": {"nodes": [{"number": 7}], "totalCount": 1}, "blocking": {"nodes": [{"number": 10}], "totalCount": 1}}'
    )
    comments = '{"id": 1, "author": "bob", "created_at": "2026-01-02", "url": "https://github.com/owner/repo/issues/42#issuecomment-1", "body": "hi"}'
    cmds = _fake_gh(monkeypatch, {"issue view": view, "api --paginate": comments})

    details = get_details("gh", "owner", "repo", 42)

    assert cmds[0][:5] == ["gh", "issue", "view", "42", "--repo"]
    assert cmds[0][5] == "owner/repo"
    assert (details.number, details.title, details.state, details.body) == (42, "Widget", "OPEN", "It breaks")
    assert details.tags == ["bug"]
    assert details.fields == {"Assignees": "jane", "Milestone": "v1"}
    assert (details.parent, details.children, details.blocked_by, details.blocks) == (5, [9], [7], [10])
    assert [(c.id, c.author, c.body) for c in details.comments] == [(1, "bob", "hi")]


def test_get_details_of_a_bare_issue(monkeypatch) -> None:
    view = (
        '{"number": 1, "title": "t", "state": "CLOSED", "url": "u", "body": "", "labels": [], "assignees": [], "milestone": null,'
        ' "parent": null, "subIssues": {"nodes": [], "totalCount": 0}, "blockedBy": {"nodes": [], "totalCount": 0},'
        ' "blocking": {"nodes": [], "totalCount": 0}}'
    )
    _fake_gh(monkeypatch, {"issue view": view})

    details = get_details("gh", "owner", "repo", 1)

    assert details.parent is None
    assert details.children == details.blocked_by == details.blocks == []
    assert details.comments == []
