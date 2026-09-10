from unittest.mock import MagicMock

import pytest

from bmsdna.devtools.gh_issue import build_search_query, parse_comment_id, parse_issue_number, search, update


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


def test_build_search_query_quotes_keyword_that_looks_like_a_qualifier() -> None:
    # A bare `sort:created-asc` or `is:pr` keyword would otherwise be parsed by GitHub's
    # search syntax as a qualifier rather than searched for literally.
    assert build_search_query(["sort:created-asc"], None) == '"sort:created-asc" sort:updated-desc'
    assert build_search_query(["is:pr"], None) == '"is:pr" sort:updated-desc'


def test_build_search_query_does_not_quote_plain_keywords() -> None:
    # Existing, working behavior for ordinary keywords must be unaffected.
    assert build_search_query(["auth", "timeout"], None) == "auth timeout sort:updated-desc"


def test_search_defaults_to_open_state(monkeypatch) -> None:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    search("gh", [], None, 10)

    assert captured_cmd[captured_cmd.index("--state") + 1] == "open"


def test_search_passes_through_requested_state(monkeypatch) -> None:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_issue.subprocess.run", fake_run)

    search("gh", ["auth"], None, 10, state="all")

    assert captured_cmd[captured_cmd.index("--state") + 1] == "all"


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
