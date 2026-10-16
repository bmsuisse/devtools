import json

from bmsdna.devtools.issue_show import IssueDetails, format_details
from bmsdna.devtools.issue_watch import Comment

FULL = IssueDetails(
    number=12,
    title="Widget is broken",
    state="Active",
    url="https://example.test/12",
    body="It breaks",
    tags=["a", "b"],
    fields={"Type": "Bug", "Iteration": "MyProj\\Sprint 1", "Assigned to": None},
    parent=5,
    children=[9, 10],
    blocked_by=[7],
    blocks=[11],
    related=[3],
    comments=[Comment(1, "Bob", "2026-01-02", "https://example.test/12#c1", "first")],
)


def test_format_details_text_shows_everything_that_is_set() -> None:
    text = format_details(FULL)

    assert text.splitlines()[:2] == ["#12 [Active] Widget is broken", "https://example.test/12"]
    for line in (
        "Type: Bug", "Iteration: MyProj\\Sprint 1", "Tags: a, b", "Parent: #5", "Children: #9, #10",
        "Blocked by: #7", "Blocks: #11", "Related: #3", "It breaks", "Comments (1):", "Comment #1 by Bob at 2026-01-02",
    ):
        assert line in text.splitlines()
    assert "Assigned to" not in text  # empty values are left out


def test_format_details_text_of_a_bare_item_leaves_out_empty_sections() -> None:
    text = format_details(IssueDetails(number=1, title="t", state="New", url="u", body="", tags=[]))

    assert text == "#1 [New] t\nu\n\n(no description)"


def test_format_details_json_is_one_document_with_all_fields() -> None:
    data = json.loads(format_details(FULL, as_json=True))

    assert data["number"] == 12
    assert data["parent"] == 5
    assert data["blocked_by"] == [7]
    assert data["fields"]["Type"] == "Bug"
    assert data["comments"] == [{"id": 1, "author": "Bob", "created_at": "2026-01-02", "url": "https://example.test/12#c1", "body": "first"}]


def test_format_details_text_escapes_terminal_control_characters_but_json_keeps_them() -> None:
    evil = IssueDetails(number=1, title="t\x1b[2J", state="New", url="u", body="line1\nline2\ttab\x07", tags=[])

    text = format_details(evil)

    assert "\x1b" not in text and "\x07" not in text
    assert "t\\x1b[2J" in text
    assert "line1\nline2\ttab" in text
    assert json.loads(format_details(evil, as_json=True))["title"] == "t\x1b[2J"
