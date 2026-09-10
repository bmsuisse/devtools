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


def test_update_does_not_claim_success_when_only_a_comment_was_posted(monkeypatch, capsys) -> None:
    # 'Active' has no GitHub equivalent, so _set_state only posts an explanatory comment — the
    # issue itself is unchanged, so "Updated issue #42" would be misleading here.
    _run_update_capturing_commands(monkeypatch, state="Active")

    out = capsys.readouterr().out
    assert "Updated issue #42" not in out
    assert "Issue #42: 'Active' isn't a valid GitHub issue state — noted in a comment." in out


def test_update_reports_success_when_state_synonym_applies(monkeypatch, capsys) -> None:
    _run_update_capturing_commands(monkeypatch, state="Closed")

    out = capsys.readouterr().out
    assert "Updated issue #42" in out


def test_update_reports_success_when_other_fields_change_even_if_state_is_unsupported(monkeypatch, capsys) -> None:
    _run_update_capturing_commands(monkeypatch, title="New title", state="Active")

    out = capsys.readouterr().out
    assert "Updated issue #42" in out
