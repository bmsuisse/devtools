"""The `CANCELLED BUILD:` / `ABANDONED PR:` PR comments `bdt pr cancel-build` / `bdt pr abandon` leave, and reading them
back for `pr info` / `pr status`.

The comment is how a cancellation or abandonment gets its reason (and who -- which agent session -- did it) onto the PR;
neither Azure DevOps nor GitHub has a field for that:

    CANCELLED BUILD: <reason>

    Cancelled build(s):
    - <pipeline> #<id>: <url>

    <Agent> Session: <id or url>

The build ids tie a comment to the builds it cancelled, so a reason is only shown for a build that is cancelled *now*
-- not for one somebody else cancelled later, after the one the comment is about was already requeued. A comment
without ids (written by hand) is taken to be about whatever is cancelled. An `ABANDONED PR:` comment is the same
without the build list (a PR is abandoned once). The session line is the one every
bdt-written comment ends in under a coding agent (`ensure_agent_session_note`: Claude, Codex, ...), or the one given
with `--session`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from .cli_tools import detect_agent_session

PREFIX = "CANCELLED BUILD:"
ABANDON_PREFIX = "ABANDONED PR:"
_BUILDS_HEADER = "Cancelled build(s):"
_BUILD_LINE_RE = re.compile(r"^- .* #(\d+): \S+$", re.MULTILINE)
_SESSION_LINE_RE = re.compile(r"^([A-Za-z][\w .-]{0,30}?) Session: (\S+)$", re.MULTILINE)


@dataclass(frozen=True)
class Session:
    """The coding-agent session that did something: `agent` is "Claude", "Codex", ... ("Agent" if unknown)."""

    agent: str
    id: str  # an id, or a link to the session


@dataclass(frozen=True)
class Reason:
    """Why a build was cancelled / a PR abandoned, and by which agent session if the comment says."""

    reason: str
    session: Session | None = None


def resolve_session(explicit: str | None) -> Session | None:
    """The session to record: `explicit` (`--session`, an id or url) if given, else the agent bdt is running under, else
    None. An explicit value is labelled with the detected agent's name -- so `--session <id>` from within Codex says
    Codex -- or Claude for a claude.ai link, or just "Agent"."""
    detected = detect_agent_session()
    if explicit is None:
        return Session(detected.name, detected.session_id) if detected else None
    value = explicit.strip()
    if not value or len(value.split()) != 1:
        raise ValueError("a session is a single id or URL, without spaces")
    if "claude.ai" in value:
        return Session("Claude", value)
    return Session(detected.name if detected else "Agent", value)


def comment_body(reason: str, builds: Iterable[tuple[str, int, str]], session: Session | None = None) -> str:
    """The comment text for `reason`; `builds` are (pipeline/workflow name, id, url)."""
    lines = "\n".join(f"- {name} #{build_id}: {url}" for name, build_id, url in builds)
    body = f"{PREFIX} {reason.strip()}\n\n{_BUILDS_HEADER}\n{lines}"
    return f"{body}\n\n{session.agent} Session: {session.id}" if session else body


def one_line(text: str) -> str:
    """`text` as one printable line: it is attacker-controlled when the PR is someone else's, and goes to a terminal."""
    return " ".join("".join(c for c in text if unicodedata.category(c)[0] != "C" or c.isspace()).split())


def abandon_body(reason: str, session: Session | None = None) -> str:
    """The comment text for abandoning a PR for `reason`."""
    body = f"{ABANDON_PREFIX} {reason.strip()}"
    return f"{body}\n\n{session.agent} Session: {session.id}" if session else body


def parse(comment: str, prefix: str = PREFIX) -> tuple[Reason, set[int]] | None:
    """(what the comment says, ids of the builds it is about) of a `CANCELLED BUILD:` (or other `prefix`) comment,
    None for any other comment."""
    text = comment.strip()
    if not text.startswith(prefix):
        return None
    reason_part, _, rest = text[len(prefix) :].partition("\n\n")
    ids: set[int] = set()
    if rest.lstrip().startswith(_BUILDS_HEADER):
        # only the lines `comment_body` writes count: a stray "#123" in a note is no build id
        ids = {int(i) for i in _BUILD_LINE_RE.findall(rest.partition(_BUILDS_HEADER)[2].split("\n\n")[0])}
    # the first one: `--session` is written before the footer of whoever ran the command
    match = _SESSION_LINE_RE.search(rest)
    session = Session(one_line(match.group(1)), one_line(match.group(2))) if match else None
    return Reason(one_line(reason_part) or "(no reason given)", session), ids


def latest(comments_oldest_first: Iterable[str], cancelled_ids: set[int]) -> Reason | None:
    """The newest `CANCELLED BUILD:` comment about one of the `cancelled_ids` builds (or about none in particular);
    None if there is no such comment."""
    for comment in reversed(list(comments_oldest_first)):
        parsed = parse(comment)
        if parsed is None:
            continue
        reason, ids = parsed
        if not ids or ids & cancelled_ids:
            return reason
    return None


def latest_abandonment(comments_oldest_first: Iterable[str]) -> Reason | None:
    """The newest `ABANDONED PR:` comment; None if there is none."""
    for comment in reversed(list(comments_oldest_first)):
        parsed = parse(comment, ABANDON_PREFIX)
        if parsed is not None:
            return parsed[0]
    return None


REQUEUE_HINT = "Run `bdt pr requeue-build` to queue it again."


def _notice(head: str, reason: Reason, by: str) -> str:
    text = f"{head}: {reason.reason}"
    return f"{text}\n{by} {reason.session.agent} session: {reason.session.id}" if reason.session else text


def cancelled_notice(cancellation: Reason | None) -> str:
    """The text `pr status` / `pr info` show for a cancelled build."""
    if cancellation is None:
        return f"Build cancelled (no '{PREFIX}' comment on the PR says why)."
    return _notice("Build cancelled", cancellation, "Cancelled by")


def abandoned_notice(abandonment: Reason | None) -> str:
    """The text `pr status` / `pr info` show for an abandoned PR."""
    if abandonment is None:
        return f"PR abandoned (no '{ABANDON_PREFIX}' comment on the PR says why)."
    return _notice("PR abandoned", abandonment, "Abandoned by")
