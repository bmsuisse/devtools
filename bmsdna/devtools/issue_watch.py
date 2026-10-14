"""`bdt issue watch <number>`: block until a new comment arrives on an issue / work item.

Lets an agent ask a question in an issue and wait for the answer: post the question with
`bdt issue comment add`, then `bdt issue watch` returns (exit 0) with the comment(s) that came in
since -- or exits `EXIT_TIMEOUT` if none did within `--timeout`.

Everything here is backend-agnostic: the caller injects a `fetch` that lists the issue's comments
(`gh_issue.list_comments` / `ado_issue.list_comments`), plus the clock and `sleep` in tests.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from .agent_mentions import mentions_agent

# `issue watch` exits with this code when --timeout passed without a (matching) new comment.
# Deliberately not 1 (an error), 2 (usage error, Click/Typer) or 3 (`pr status --wait`: needs approval).
EXIT_TIMEOUT = 4

# Defaults of `--interval` / `--timeout` of `issue watch` and `issue search --wait`.
DEFAULT_INTERVAL = 20.0
DEFAULT_TIMEOUT = 900.0

# Consecutive failed polls tolerated before giving up: a long watch shouldn't die of one network blip.
MAX_CONSECUTIVE_ERRORS = 3

_HTML_BREAK_RE = re.compile(r"(?i)<br\s*/?>|</(?:div|p|li)>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Comment:
    id: int
    author: str
    created_at: str
    url: str
    body: str


def to_comments(raw: list[dict], *, is_html: bool = False) -> list[Comment]:
    """The backends' `list_comments` dicts as `Comment`s; `is_html` converts Azure DevOps's HTML bodies to text."""
    return [Comment(int(c["id"]), c["author"], c["created_at"], c["url"], html_to_text(c["body"]) if is_html else c["body"]) for c in raw]


def build_accept(
    *,
    agent: str | None = None,
    for_me: bool = False,
    own_session_id: str | None = None,
    own_author: str | None = None,
) -> Callable[[Comment], bool]:
    """Which new comments are worth returning: with `for_me`, only those mentioning `@<agent>` /
    `@Agent`; a comment this caller wrote itself is skipped when `own_session_id` (bdt appends the
    agent's session note to every comment it posts) is in it, or `own_author` wrote it."""

    def accept(comment: Comment) -> bool:
        if for_me and not mentions_agent(comment.body, agent):
            return False
        if own_session_id and own_session_id in comment.body:
            return False
        if own_author and comment.author.casefold() == own_author.casefold():
            return False
        return True

    return accept


def poll_until[T](
    probe: Callable[[], T | None],
    *,
    interval: float = DEFAULT_INTERVAL,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True),
) -> T | None:
    """Call `probe` (immediately, then every `interval` seconds) until it returns something other than None;
    return that. None when `timeout` seconds (None = never) pass first.

    A failing probe is retried, `MAX_CONSECUTIVE_ERRORS` failures in a row are raised: a long poll
    shouldn't die of one network blip, but must not turn a persistent failure into "nothing found".
    """
    deadline = None if timeout is None else clock() + timeout
    errors = 0
    while True:
        try:
            result = probe()
            errors = 0
        except (Exception, SystemExit) as e:
            errors += 1
            if errors >= MAX_CONSECUTIVE_ERRORS:
                raise
            log(f"Polling failed ({e}); retrying ({errors}/{MAX_CONSECUTIVE_ERRORS - 1})")
        else:
            if result is not None:
                return result
        if deadline is not None:
            remaining = deadline - clock()
            if remaining <= 0:
                return None
            sleep(min(interval, remaining))
        else:
            sleep(interval)


def watch(
    fetch: Callable[[], list[Comment]],
    *,
    since_id: int | None = None,
    accept: Callable[[Comment], bool] = lambda c: True,
    interval: float = DEFAULT_INTERVAL,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True),
) -> list[Comment] | None:
    """Poll `fetch` every `interval` seconds until comments newer than the baseline that `accept`
    appear; return them (oldest first). None when `timeout` seconds (None = never) pass first.

    The baseline is `since_id` (a comment id: everything after it counts, even if it was already
    posted before the watch started) or, by default, the newest comment existing at the start.
    """
    baseline = since_id

    def probe() -> list[Comment] | None:
        nonlocal baseline
        comments = fetch()
        if baseline is None:
            baseline = max((c.id for c in comments), default=0)
            return None
        return [c for c in comments if c.id > baseline and accept(c)] or None

    return poll_until(probe, interval=interval, timeout=timeout, sleep=sleep, clock=clock, log=log)


def html_to_text(text: str) -> str:
    """An Azure DevOps comment (HTML) as plain text; GitHub comments (Markdown) pass through unchanged."""
    return html_lib.unescape(_HTML_TAG_RE.sub("", _HTML_BREAK_RE.sub("\n", text))).strip()


def format_comment(comment: Comment, *, as_json: bool = False) -> str:
    """One comment as a JSON line (`as_json`) or a short human-readable block."""
    if as_json:
        return json.dumps(asdict(comment), ensure_ascii=False)
    return f"Comment #{comment.id} by {comment.author} at {comment.created_at}\n{comment.url}\n\n{comment.body}"
