import pytest

from bmsdna.devtools.gh_issue import build_search_query, parse_comment_id, parse_issue_number


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
