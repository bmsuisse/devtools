import json

import pytest

from bmsdna.devtools.issue_watch import (
    MAX_CONSECUTIVE_ERRORS,
    Comment,
    build_accept,
    format_comment,
    html_to_text,
    to_comments,
    watch,
)


def c(id: int, body: str = "hello", author: str = "alice") -> Comment:
    return Comment(id, author, "2026-10-13T08:00:00Z", f"https://example.test/issues/1#c{id}", body)


class FakeClock:
    """A clock whose `sleep` advances it, so a watch runs instantly and deterministically."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs


def sequence(*polls):
    """A fetcher returning each of `polls` in turn (the last one repeats); an Exception is raised instead."""
    it = iter(polls)
    last = []

    def fetch():
        nonlocal last
        try:
            last = next(it)
        except StopIteration:
            pass
        if isinstance(last, BaseException):
            raise last
        return last

    return fetch


def run(fetch, clock: FakeClock, **kwargs):
    return watch(fetch, sleep=clock.sleep, clock=clock.clock, log=lambda msg: None, **kwargs)


def test_returns_comment_that_arrives_after_start() -> None:
    clock = FakeClock()
    fetch = sequence([c(1)], [c(1)], [c(1), c(2, "answer")])

    result = run(fetch, clock, interval=10, timeout=100)

    assert result == [c(2, "answer")]
    assert clock.sleeps == [10, 10]


def test_existing_comments_are_not_reported_by_default() -> None:
    clock = FakeClock()
    assert run(sequence([c(1), c(2)]), clock, interval=10, timeout=25) is None


def test_since_id_reports_comments_posted_before_the_watch_started() -> None:
    clock = FakeClock()
    result = run(sequence([c(1), c(2), c(3)]), clock, since_id=1, interval=10, timeout=100)
    assert result == [c(2), c(3)]
    assert clock.sleeps == []


def test_empty_issue_then_first_comment() -> None:
    clock = FakeClock()
    result = run(sequence([], [], [c(7)]), clock, interval=5, timeout=100)
    assert result == [c(7)]


def test_times_out_returning_none_without_oversleeping() -> None:
    clock = FakeClock()
    result = run(sequence([c(1)]), clock, interval=10, timeout=25)
    assert result is None
    assert clock.sleeps == [10, 10, 5]
    assert clock.now == 1025.0


def test_no_timeout_keeps_polling_until_a_comment_arrives() -> None:
    clock = FakeClock()
    polls = [[c(1)]] * 50 + [[c(1), c(2)]]
    assert run(sequence(*polls), clock, interval=60, timeout=None) == [c(2)]


def test_accept_filters_comments_and_keeps_waiting() -> None:
    clock = FakeClock()
    accept = build_accept(agent="claude", for_me=True)
    fetch = sequence([c(1)], [c(1), c(2, "unrelated")], [c(1), c(2, "unrelated"), c(3, "@Claude thoughts?")])

    result = run(fetch, clock, accept=accept, interval=10, timeout=100)

    assert result == [c(3, "@Claude thoughts?")]


def test_a_transient_error_is_retried() -> None:
    clock = FakeClock()
    fetch = sequence([c(1)], RuntimeError("boom"), [c(1), c(2)])
    assert run(fetch, clock, interval=10, timeout=100) == [c(2)]


def test_repeated_errors_are_raised() -> None:
    clock = FakeClock()
    fetch = sequence([c(1)], *[RuntimeError("down")] * MAX_CONSECUTIVE_ERRORS)
    with pytest.raises(RuntimeError, match="down"):
        run(fetch, clock, interval=10, timeout=1000)


def test_gh_style_sys_exit_is_retried_too() -> None:
    # `gh_issue._run_gh` reports a failed `gh` call with sys.exit
    clock = FakeClock()
    fetch = sequence([c(1)], SystemExit("gh: network"), [c(1), c(2)])
    assert run(fetch, clock, interval=10, timeout=100) == [c(2)]


# -- build_accept ----------------------------------------------------------------


def test_accept_everything_by_default() -> None:
    assert build_accept()(c(1, "anything"))


def test_for_me_requires_a_mention() -> None:
    accept = build_accept(agent="codex", for_me=True)
    assert accept(c(1, "@codex look"))
    assert accept(c(1, "@Agent look"))
    assert not accept(c(1, "@claude look"))


def test_skip_own_by_session_id() -> None:
    accept = build_accept(own_session_id="https://claude.ai/code/abc")
    assert not accept(c(1, "my question\n\nClaude Session: https://claude.ai/code/abc"))
    assert accept(c(2, "an answer"))


def test_skip_own_by_author_is_case_insensitive() -> None:
    accept = build_accept(own_author="Alice")
    assert not accept(c(1, author="alice"))
    assert accept(c(2, author="bob"))


# -- formatting --------------------------------------------------------------------


def test_format_comment_json_is_one_parsable_line() -> None:
    line = format_comment(c(5, 'multi\nline "quoted"'), as_json=True)
    assert "\n" not in line
    assert json.loads(line) == {
        "id": 5,
        "author": "alice",
        "created_at": "2026-10-13T08:00:00Z",
        "url": "https://example.test/issues/1#c5",
        "body": 'multi\nline "quoted"',
    }


def test_format_comment_text() -> None:
    text = format_comment(c(5, "the answer"))
    assert text.splitlines()[0] == "Comment #5 by alice at 2026-10-13T08:00:00Z"
    assert text.endswith("the answer")


def test_html_to_text_strips_tags_and_unescapes() -> None:
    assert html_to_text('<div>Hi <a href="x">@Claude</a><br>a &amp; b</div><div>next</div>') == "Hi @Claude\na & b\nnext"


def test_to_comments_converts_html_only_when_asked() -> None:
    raw = [{"id": "3", "author": "a", "created_at": "t", "url": "u", "body": "<div>x</div>"}]
    assert to_comments(raw)[0].body == "<div>x</div>"
    assert to_comments(raw, is_html=True) == [Comment(3, "a", "t", "u", "x")]
