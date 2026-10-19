import pytest
from fake_github import NOT_FOUND, FakeGitHub

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
    delete,
    last_comment,
    list_comments,
    resolve_board,
    resolve_project_number,
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



SEARCH = "GET search/issues"


def _search_hit(number: int, state: str = "open", repo: str = "repo") -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "html_url": f"https://github.com/owner/{repo}/issues/{number}",
        "state": state,
        "repository_url": f"https://api.github.com/repos/owner/{repo}",
    }


def _query(gh: FakeGitHub) -> dict:
    return gh.calls_to("GET", "search/issues")[0]


def _projects_graphql(projects: list[dict], items: list[dict] | None = None):
    def handler(query: str, variables: dict) -> dict:
        if "projectsV2" in query:
            return {"repositoryOwner": {"projectsV2": {"nodes": projects, "pageInfo": {"hasNextPage": False}}}}
        assert "items(" in query
        return {"repositoryOwner": {"projectV2": {"items": {"nodes": items or [], "pageInfo": {"hasNextPage": False}}}}}

    return handler


def test_search_defaults_to_open_state() -> None:
    gh = FakeGitHub({SEARCH: []})
    search(gh, "owner", "repo", [], None, 10)
    params = _query(gh)["params"]
    assert "is:open" in params["q"].split() and "repo:owner/repo" in params["q"].split() and "is:issue" in params["q"].split()
    assert (params["sort"], params["order"]) == ("updated", "desc")
    assert "sort:updated-desc" not in params["q"]


def test_search_all_state_has_no_state_qualifier() -> None:
    gh = FakeGitHub({SEARCH: []})
    search(gh, "owner", "repo", ["auth"], None, 10, state="all")
    q = _query(gh)["params"]["q"].split()
    assert "is:open" not in q and "is:closed" not in q and "auth" in q


def test_search_passes_through_requested_state_and_since() -> None:
    gh = FakeGitHub({SEARCH: []})
    search(gh, "owner", "repo", [], "2026-01-01", 10, state="closed")
    q = _query(gh)["params"]["q"].split()
    assert "is:closed" in q and "updated:>=2026-01-01" in q


def test_search_uppercases_state_for_the_current_repo(capsys) -> None:
    gh = FakeGitHub({SEARCH: [_search_hit(1)]})
    results = search(gh, "owner", "repo", [], None, 10)
    assert results[0]["state"] == "OPEN"
    assert "#1 [OPEN] issue 1" in capsys.readouterr().out


def test_search_board_overfetches_then_filters_to_limit() -> None:
    hits = [_search_hit(i) for i in range(1, 21)]
    # Board only has the even-numbered issues on it.
    items = [{"content": {"__typename": "Issue", "number": i, "repository": {"nameWithOwner": "owner/repo"}}} for i in range(2, 21, 2)]
    gh = FakeGitHub({SEARCH: hits}, graphql=_projects_graphql([{"id": "P1", "title": "Roadmap", "number": 7}], items))

    results = search(gh, "owner", "repo", [], None, 3, board="Roadmap")

    assert gh.calls_to("GET", "search/issues")[0]["limit"] == 30  # limit * _BOARD_SEARCH_OVERFETCH
    assert len(results) == 3
    assert all(item["number"] % 2 == 0 for item in results)


def test_search_board_by_number_skips_the_title_lookup() -> None:
    items = [{"content": {"__typename": "Issue", "number": 1, "repository": {"nameWithOwner": "owner/repo"}}}]
    gh = FakeGitHub({SEARCH: [_search_hit(1), _search_hit(2)]}, graphql=_projects_graphql([], items))
    results = search(gh, "owner", "repo", [], None, 10, board="7")
    assert [r["number"] for r in results] == [1]
    assert not any("projectsV2" in q for m, q, _ in gh.calls if m == "GRAPHQL")


def test_search_org_wide_searches_the_owner_and_keeps_lowercase_state(capsys) -> None:
    gh = FakeGitHub({SEARCH: [_search_hit(5, repo="other-repo")]})

    results = search(gh, "owner", "repo", ["auth"], None, 10, org_wide=True)

    q = _query(gh)["params"]["q"].split()
    assert "user:owner" in q and "repo:owner/repo" not in q
    assert results == [
        {"number": 5, "title": "issue 5", "url": "https://github.com/owner/other-repo/issues/5", "state": "open", "repository": {"nameWithOwner": "owner/other-repo"}}
    ]
    assert "owner/other-repo#5 [open] issue 5" in capsys.readouterr().out


def test_search_org_wide_all_state_omits_state_qualifier() -> None:
    gh = FakeGitHub({SEARCH: []})
    search(gh, "owner", "repo", [], None, 10, state="all", org_wide=True)
    q = _query(gh)["params"]["q"].split()
    assert "is:open" not in q and "is:closed" not in q


@pytest.mark.parametrize("org_wide", [True, False])
def test_search_passes_labels_as_anded_qualifiers(org_wide: bool) -> None:
    gh = FakeGitHub({SEARCH: []})
    search(gh, "owner", "repo", [], None, 10, org_wide=org_wide, labels=["bug", "needs info"])
    q = _query(gh)["params"]["q"]
    assert "label:bug" in q.split() and 'label:"needs info"' in q


def test_search_org_wide_and_board_together_exits() -> None:
    with pytest.raises(SystemExit):
        search(FakeGitHub(), "owner", "repo", [], None, 10, board="Roadmap", org_wide=True)


def test_search_board_ignores_same_number_issue_from_a_different_repo() -> None:
    # This repo's own #7, plus a same-numbered #7 from an unrelated repo that also happens to
    # be on the (org-wide) board -- only the former should count as a match.
    items = [{"content": {"__typename": "Issue", "number": 7, "repository": {"nameWithOwner": "owner/other-repo"}}}]
    gh = FakeGitHub({SEARCH: [_search_hit(7)]}, graphql=_projects_graphql([], items))
    assert search(gh, "owner", "repo", [], None, 10, board="7") == []


def test_board_items_drop_pull_requests_and_drafts() -> None:
    items = [
        {"content": {"__typename": "Issue", "number": 1, "repository": {"nameWithOwner": "owner/repo"}}},
        {"content": {"__typename": "PullRequest"}},
        {"content": None},
    ]
    from bmsdna.devtools.gh_issue import board_issue_numbers

    gh = FakeGitHub(graphql=_projects_graphql([], items))
    assert board_issue_numbers(gh, "owner", "repo", 7) == {1}


def test_resolve_project_number_looks_the_title_up_case_insensitively() -> None:
    gh = FakeGitHub(graphql=_projects_graphql([{"id": "P", "title": "Roadmap", "number": 7}]))
    assert resolve_project_number(gh, "owner", "roadmap") == 7


def test_resolve_project_number_exits_for_an_unknown_board() -> None:
    gh = FakeGitHub(graphql=_projects_graphql([{"id": "P", "title": "Backlog", "number": 3}]))
    with pytest.raises(SystemExit, match="not found"):
        resolve_project_number(gh, "owner", "Roadmap")


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



def _update_gh(**routes) -> FakeGitHub:
    return FakeGitHub({"GET repos/owner/repo/issues/42": {"id": 4200, "node_id": "N42"}, **routes})


@pytest.mark.parametrize("state", ["Closed", "Done", "completed"])
def test_update_closes_as_completed_for_done_states(state: str) -> None:
    gh = FakeGitHub()
    update(gh, 42, state=state)
    assert gh.calls == [("PATCH", "repos/owner/repo/issues/42", {"state": "closed", "state_reason": "completed"})]


@pytest.mark.parametrize("state", ["Removed", "Not Planned", "wontfix"])
def test_update_closes_as_not_planned_for_removed_states(state: str) -> None:
    gh = FakeGitHub()
    update(gh, 42, state=state)
    assert gh.calls == [("PATCH", "repos/owner/repo/issues/42", {"state": "closed", "state_reason": "not_planned"})]


def test_update_reopens_for_open_state() -> None:
    gh = FakeGitHub()
    update(gh, 42, state="Open")
    assert gh.calls == [("PATCH", "repos/owner/repo/issues/42", {"state": "open"})]


def test_update_comments_exact_state_when_unsupported() -> None:
    gh = FakeGitHub()
    update(gh, 42, state="Active")
    assert gh.calls == [
        (
            "POST",
            "repos/owner/repo/issues/42/comments",
            {"body": "Requested state change to 'Active', which isn't a valid GitHub issue state — left unchanged."},
        )
    ]


def test_update_errors_with_nothing_to_do() -> None:
    gh = FakeGitHub()
    with pytest.raises(SystemExit):
        update(gh, 42)
    assert gh.calls == []


def test_update_edits_fields_and_labels() -> None:
    gh = FakeGitHub()
    update(gh, 42, title="New", body="text", add_labels=["a", "b c"], remove_labels=["old/x"])
    assert gh.calls == [
        ("PATCH", "repos/owner/repo/issues/42", {"title": "New", "body": "text"}),
        ("POST", "repos/owner/repo/issues/42/labels", {"labels": ["a", "b c"]}),
        ("DELETE", "repos/owner/repo/issues/42/labels/old%2Fx", None),
    ]


def _fake_push_assets(monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.gh_issue.push_assets",
        lambda owner, repo, key, paths, **kwargs: [f"https://github.com/{owner}/{repo}/blob/pr-assets/{key}/{i:02d}-{p.split('/')[-1]}?raw=true" for i, p in enumerate(paths)],
    )


CREATED = {"number": 42, "id": 4200, "node_id": "N42", "html_url": "https://github.com/owner/repo/issues/42"}
CREATE = "POST repos/owner/repo/issues"


def test_create_posts_the_issue_with_labels_and_prints_the_url(capsys) -> None:
    gh = FakeGitHub({CREATE: CREATED})

    create(gh, "owner", "repo", "Bug title", "body text", ["bug", "ui"], [], [])

    assert gh.calls == [("POST", "repos/owner/repo/issues", {"title": "Bug title", "body": "body text", "labels": ["bug", "ui"]})]
    assert "https://github.com/owner/repo/issues/42" in capsys.readouterr().out


def test_create_with_files_appends_attachments_section(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    gh = FakeGitHub({CREATE: CREATED})

    create(gh, "owner", "repo", "Bug title", "body text", [], [], [], file_paths=["/tmp/report.pdf"])

    body = gh.calls_to("PATCH", "repos/owner/repo/issues/42")[0]["body"]
    assert "## Attachments" in body
    assert "[report.pdf]" in body
    assert "body text" in body


def test_create_maps_assignee_and_milestone_extra_args() -> None:
    gh = FakeGitHub({CREATE: CREATED, "GET repos/owner/repo/milestones": [{"title": "v1", "number": 3}], "GET user": {"login": "me"}})

    create(gh, "owner", "repo", "T", "b", [], [], ["--assignee", "jane,@me", "--milestone=V1"])

    payload = gh.calls_to("POST", "repos/owner/repo/issues")[0]
    assert payload["assignees"] == ["jane", "me"] and payload["milestone"] == 3


def test_create_rejects_unsupported_extra_args() -> None:
    with pytest.raises(SystemExit, match="Unsupported"):
        create(FakeGitHub({CREATE: CREATED}), "owner", "repo", "T", "b", [], [], ["--web"])


def test_create_with_board_adds_the_issue_to_the_project() -> None:
    gh = FakeGitHub({CREATE: CREATED}, graphql=_projects_graphql([{"id": "PVT_1", "title": "Roadmap", "number": 7}]) )
    base = gh.graphql_handler
    gh.graphql_handler = lambda q, v: {} if "addProjectV2ItemById" in q else base(q, v)

    create(gh, "owner", "repo", "Bug title", "body text", [], [], [], board="Roadmap")

    add = next(v for m, q, v in gh.calls if m == "GRAPHQL" and "addProjectV2ItemById" in q)
    assert add == {"project": "PVT_1", "content": "N42"}


def test_create_with_unknown_board_fails_before_creating_the_issue() -> None:
    gh = FakeGitHub({CREATE: CREATED}, graphql=_projects_graphql([]))
    with pytest.raises(SystemExit, match="not found"):
        create(gh, "owner", "repo", "T", "b", [], [], [], board="Nope")
    assert gh.calls_to("POST") == []


def test_create_without_board_makes_no_graphql_calls() -> None:
    gh = FakeGitHub({CREATE: CREATED})
    create(gh, "owner", "repo", "Bug title", "body text", [], [], [])
    assert not any(m == "GRAPHQL" for m, _, _ in gh.calls)


def test_create_with_parent_and_blockers_calls_the_link_endpoints() -> None:
    gh = FakeGitHub(
        {
            CREATE: CREATED,
            "GET repos/owner/repo/issues/7": {"id": 700},
            "GET repos/owner/repo/issues/8": {"id": 800},
        }
    )

    create(gh, "owner", "repo", "Title", "body", [], [], [], parent=5, blocked_by=[7, 8, 7])

    assert gh.calls_to("POST", "repos/owner/repo/issues/5/sub_issues") == [{"sub_issue_id": 4200}]
    assert gh.calls_to("POST", "repos/owner/repo/issues/42/dependencies/blocked_by") == [{"issue_id": 700}, {"issue_id": 800}]


def test_create_without_links_makes_no_link_calls() -> None:
    gh = FakeGitHub({CREATE: CREATED})
    create(gh, "owner", "repo", "Title", "body", [], [], [])
    assert len(gh.calls) == 1


def test_comment_with_screenshots_and_files_builds_both_sections(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    gh = FakeGitHub({"POST repos/owner/repo/issues/42/comments": {"id": 1, "html_url": "https://github.com/owner/repo/issues/42#issuecomment-1"}})

    comment(gh, "owner", "repo", 42, "Fixed", ["/tmp/shot.png"], ["/tmp/report.pdf"])

    body = gh.calls[0][2]["body"]
    assert "Fixed" in body
    assert "## Screenshots" in body
    assert "## Attachments" in body


def test_comment_prints_id_and_url(capsys) -> None:
    gh = FakeGitHub({"POST repos/owner/repo/issues/7/comments": {"id": 1, "html_url": "https://github.com/owner/repo/issues/7#issuecomment-1"}})
    comment(gh, "owner", "repo", 7, "Taken by me", [], agent_note=False)
    out = capsys.readouterr().out
    assert "(comment #1)" in out and "issuecomment-1" in out
    assert gh.calls == [("POST", "repos/owner/repo/issues/7/comments", {"body": "Taken by me"})]


def test_comment_names_its_repo_explicitly() -> None:
    gh = FakeGitHub({"POST repos/other/place/issues/7/comments": {"id": 1, "html_url": "u"}})
    comment(gh, "other", "place", 7, "hi", [], agent_note=False)
    assert gh.calls[0][1] == "repos/other/place/issues/7/comments"


def test_create_appends_agent_session_note_when_detected(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    gh = FakeGitHub({CREATE: CREATED})

    create(gh, "owner", "repo", "Bug title", "body text", [], [], [])

    assert gh.calls[0][2]["body"] == "body text\n\nClaude Session: https://claude.ai/code/session_abc123"


def test_create_without_agent_leaves_body_untouched() -> None:
    gh = FakeGitHub({CREATE: CREATED})
    create(gh, "owner", "repo", "Bug title", "body text", [], [], [])
    assert gh.calls[0][2]["body"] == "body text"


def test_comment_appends_agent_session_note_when_detected(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    gh = FakeGitHub({"POST repos/owner/repo/issues/42/comments": {"id": 1, "html_url": "u"}})

    comment(gh, "owner", "repo", 42, "Fixed", [], [])

    assert gh.calls[0][2]["body"] == "Fixed\n\nClaude Session: https://claude.ai/code/session_abc123"


# -- ensure_pr_available_label / add_pr_available_label ----------------------


def test_ensure_pr_available_label_creates_label_when_missing() -> None:
    gh = FakeGitHub({"GET repos/owner/repo/labels/pr-available": NOT_FOUND})

    ensure_pr_available_label(gh)

    (payload,) = gh.calls_to("POST", "repos/owner/repo/labels")
    assert payload["name"] == "pr-available"


def test_ensure_pr_available_label_skips_create_when_label_already_exists() -> None:
    gh = FakeGitHub({"GET repos/owner/repo/labels/pr-available": {"name": "pr-available"}})

    ensure_pr_available_label(gh)

    assert gh.calls_to("POST") == []


def test_add_pr_available_label_adds_the_label_to_the_issue() -> None:
    gh = FakeGitHub()

    add_pr_available_label(gh, 42)

    # Doesn't itself check/create the label -- that's `ensure_pr_available_label`'s job, called
    # once per `pr create` invocation rather than once per issue (see cli.py).
    assert gh.calls == [("POST", "repos/owner/repo/issues/42/labels", {"labels": ["pr-available"]})]


# -- links on update ----------------------------------------------------------

ISSUE = "repos/owner/repo/issues/42"


def test_update_links_parent_and_blockers_that_are_missing() -> None:
    gh = FakeGitHub(
        {
            f"GET {ISSUE}/parent": NOT_FOUND,
            f"GET {ISSUE}/dependencies/blocked_by": [],
            f"GET {ISSUE}": {"id": 4200},
            "GET repos/owner/repo/issues/7": {"id": 700},
            "GET repos/owner/repo/issues/8": {"id": 800},
        }
    )

    update(gh, 42, parent=5, blocked_by=[7, 8])

    assert gh.calls_to("POST", "repos/owner/repo/issues/5/sub_issues") == [{"sub_issue_id": 4200, "replace_parent": True}]
    assert gh.calls_to("POST", f"{ISSUE}/dependencies/blocked_by") == [{"issue_id": 700}, {"issue_id": 800}]
    assert gh.calls_to("PATCH") == []


def test_update_skips_links_that_already_exist(capsys) -> None:
    gh = FakeGitHub({f"GET {ISSUE}/parent": {"number": 5}, f"GET {ISSUE}/dependencies/blocked_by": [{"number": 7}]})

    update(gh, 42, parent=5, blocked_by=[7])

    assert gh.calls_to("POST") == [] and gh.calls_to("PATCH") == []
    assert "nothing to do" in capsys.readouterr().out


def test_update_only_adds_the_blockers_that_are_missing_and_replaces_a_different_parent() -> None:
    gh = FakeGitHub(
        {
            f"GET {ISSUE}/parent": {"number": 4},
            f"GET {ISSUE}/dependencies/blocked_by": [{"number": 7}],
            f"GET {ISSUE}": {"id": 4200},
            "GET repos/owner/repo/issues/8": {"id": 800},
        }
    )

    update(gh, 42, parent=5, blocked_by=[7, 8])

    assert gh.calls_to("POST", "repos/owner/repo/issues/5/sub_issues") == [{"sub_issue_id": 4200, "replace_parent": True}]
    assert gh.calls_to("POST", f"{ISSUE}/dependencies/blocked_by") == [{"issue_id": 800}]


def test_update_links_alongside_other_fields() -> None:
    gh = FakeGitHub(
        {
            f"GET {ISSUE}/dependencies/blocked_by": [],
            "GET repos/owner/repo/issues/7": {"id": 700},
        }
    )

    update(gh, 42, title="New", blocked_by=[7])

    assert gh.calls_to("PATCH", ISSUE) == [{"title": "New"}]
    assert gh.calls_to("POST", f"{ISSUE}/dependencies/blocked_by") == [{"issue_id": 700}]


def test_update_rejects_linking_an_issue_to_itself() -> None:
    gh = FakeGitHub()

    with pytest.raises(SystemExit):
        update(gh, 42, parent=42)
    assert gh.calls == []


# -- get_details / comments ---------------------------------------------------

BASE = "repos/owner/repo/issues/42"


def test_get_details_maps_the_api_responses_and_comments() -> None:
    gh = FakeGitHub(
        {
            f"GET {BASE}": {
                "number": 42, "title": "Widget", "state": "open", "html_url": "https://github.com/owner/repo/issues/42", "body": "It breaks",
                "labels": [{"name": "bug"}], "assignees": [{"login": "jane"}], "milestone": {"title": "v1"},
            },
            f"GET {BASE}/parent": {"number": 5},
            f"GET {BASE}/sub_issues": [{"number": 9}],
            f"GET {BASE}/dependencies/blocked_by": [{"number": 7}],
            f"GET {BASE}/dependencies/blocking": [{"number": 10}],
            f"GET {BASE}/comments": [
                {"id": 1, "user": {"login": "bob"}, "created_at": "2026-01-02", "html_url": "https://github.com/owner/repo/issues/42#issuecomment-1", "body": "hi"}
            ],
        }
    )

    details = get_details(gh, "owner", "repo", 42)

    assert (details.number, details.title, details.state, details.body) == (42, "Widget", "OPEN", "It breaks")
    assert details.url == "https://github.com/owner/repo/issues/42"
    assert details.tags == ["bug"]
    assert details.fields == {"Assignees": "jane", "Milestone": "v1"}
    assert (details.parent, details.children, details.blocked_by, details.blocks) == (5, [9], [7], [10])
    assert [(c.id, c.author, c.body) for c in details.comments] == [(1, "bob", "hi")]


def test_get_details_of_a_bare_issue() -> None:
    gh = FakeGitHub(
        {
            "GET repos/owner/repo/issues/1": {"number": 1, "title": "t", "state": "closed", "html_url": "u", "body": None, "labels": [], "assignees": [], "milestone": None},
            "GET repos/owner/repo/issues/1/parent": NOT_FOUND,
        }
    )

    details = get_details(gh, "owner", "repo", 1)

    assert details.state == "CLOSED" and details.body == ""
    assert details.parent is None
    assert details.children == details.blocked_by == details.blocks == []
    assert details.comments == []


def test_delete_issue_uses_the_graphql_mutation_with_its_node_id(capsys) -> None:
    gh = FakeGitHub({f"GET {BASE}": {"node_id": "I_kw"}}, graphql=lambda q, v: {})

    delete(gh, 42)

    (_, query, variables) = gh.calls[-1]
    assert "deleteIssue" in query and variables == {"issue": "I_kw"}
    assert "Deleted issue #42" in capsys.readouterr().out


def test_last_comment_reads_graphql() -> None:
    def handler(query: str, variables: dict) -> dict:
        assert variables == {"owner": "o", "repo": "r", "number": 5}
        return {"repository": {"issue": {"comments": {"nodes": [{"body": "Taken by x"}]}}}}

    assert last_comment(FakeGitHub(graphql=handler), "o", "r", 5) == "Taken by x"
    def empty(q, v):
        return {"repository": {"issue": {"comments": {"nodes": []}}}}

    assert last_comment(FakeGitHub(graphql=empty), "o", "r", 5) is None


def test_update_and_delete_comment_use_the_comment_endpoint() -> None:
    from bmsdna.devtools.gh_issue import delete_comment, update_comment

    gh = FakeGitHub()
    update_comment(gh, "o", "r", "9", "new text")
    delete_comment(gh, "o", "r", "9")
    assert gh.calls == [("PATCH", "repos/o/r/issues/comments/9", {"body": "new text"}), ("DELETE", "repos/o/r/issues/comments/9", None)]


def test_list_comments_projects_the_api_shape() -> None:
    gh = FakeGitHub({"GET repos/o/r/issues/84/comments": [
        {"id": 1, "user": {"login": "alice"}, "created_at": "t1", "html_url": "u1", "body": "first"},
        {"id": 2, "user": None, "created_at": "t2", "html_url": "u2", "body": None},
    ]})
    assert list_comments(gh, "o", "r", 84) == [
        {"id": 1, "author": "alice", "created_at": "t1", "url": "u1", "body": "first"},
        {"id": 2, "author": "ghost", "created_at": "t2", "url": "u2", "body": ""},
    ]


def test_update_remove_label_ignores_a_label_that_is_not_on_the_issue() -> None:
    from fake_github import NotFound

    gh = FakeGitHub({"DELETE repos/owner/repo/issues/42/labels/old": NotFound("Label does not exist")})

    update(gh, 42, remove_labels=["old"])  # does not raise


def test_update_remove_label_exits_when_the_issue_itself_is_missing() -> None:
    from fake_github import NotFound

    gh = FakeGitHub({"DELETE repos/owner/repo/issues/99999/labels/bug": NotFound("Not Found")})

    with pytest.raises(SystemExit, match="404"):
        update(gh, 99999, remove_labels=["bug"])


def test_search_never_asks_for_more_than_the_1000_results_the_api_serves() -> None:
    from bmsdna.devtools.gh_issue import _search_issues

    gh = FakeGitHub({SEARCH: []})

    _search_issues(gh, "", "repo:owner/repo", "open", [], 1500)

    assert gh.calls_to("GET", "search/issues")[0]["limit"] == 1000
