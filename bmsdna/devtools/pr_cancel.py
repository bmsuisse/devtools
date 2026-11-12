"""The `CANCELLED BUILD:` PR comment `bdt pr cancel-build` leaves, and reading it back for `pr info` / `pr status`.

The comment is how a cancellation gets its reason onto the PR (neither Azure DevOps nor GitHub has a field for it):

    CANCELLED BUILD: <reason>

    Cancelled build(s):
    - <pipeline> #<id>: <url>

The build ids tie a comment to the builds it cancelled, so a reason is only shown for a build that is cancelled *now*
-- not for one somebody else cancelled later, after the one the comment is about was already requeued. A comment
without ids (written by hand) is taken to be about whatever is cancelled.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

PREFIX = "CANCELLED BUILD:"
_BUILDS_HEADER = "Cancelled build(s):"
_BUILD_LINE_RE = re.compile(r"^- .* #(\d+): \S+$", re.MULTILINE)


def comment_body(reason: str, builds: Iterable[tuple[str, int, str]]) -> str:
    """The comment text for `reason`; `builds` are (pipeline/workflow name, id, url)."""
    lines = "\n".join(f"- {name} #{build_id}: {url}" for name, build_id, url in builds)
    return f"{PREFIX} {reason.strip()}\n\n{_BUILDS_HEADER}\n{lines}"


def one_line(text: str) -> str:
    """`text` as one printable line: it is attacker-controlled when the PR is someone else's, and goes to a terminal."""
    return " ".join("".join(c for c in text if unicodedata.category(c)[0] != "C" or c.isspace()).split())


def parse(comment: str) -> tuple[str, set[int]] | None:
    """(reason, ids of the builds it is about) of a `CANCELLED BUILD:` comment, None for any other comment."""
    text = comment.strip()
    if not text.startswith(PREFIX):
        return None
    reason_part, _, rest = text[len(PREFIX) :].partition("\n\n")
    ids = set()
    if rest.lstrip().startswith(_BUILDS_HEADER):
        # only the lines `comment_body` writes count: a stray "#123" in a note is no build id
        ids = {int(i) for i in _BUILD_LINE_RE.findall(rest.partition(_BUILDS_HEADER)[2].split("\n\n")[0])}
    return one_line(reason_part), ids


def latest_reason(comments_oldest_first: Iterable[str], cancelled_ids: set[int]) -> str | None:
    """The reason of the newest `CANCELLED BUILD:` comment about one of the `cancelled_ids` builds (or about none in
    particular); None if there is no such comment."""
    for comment in reversed(list(comments_oldest_first)):
        parsed = parse(comment)
        if parsed is None:
            continue
        reason, ids = parsed
        if not ids or ids & cancelled_ids:
            return reason or "(no reason given)"
    return None


REQUEUE_HINT = "Run `bdt pr requeue-build` to queue it again."


def cancelled_notice(reason: str | None) -> str:
    """The line `pr status` prints for a cancelled build."""
    if reason is None:
        return f"Build cancelled (no '{PREFIX}' comment on the PR says why)."
    return f"Build cancelled: {reason}"
