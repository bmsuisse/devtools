import pytest

from bmsdna.devtools.gh_issue import parse_comment_id, parse_issue_number


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
