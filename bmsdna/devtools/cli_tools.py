"""Locating required external CLIs (az, gh, psql) with clear errors when missing.

Uses `shutil.which` rather than a bare command name so Windows .cmd/.bat/.exe
shims (e.g. az.cmd from the MSI installer) resolve correctly via PATHEXT —
the same lookup `where`/`Get-Command` would do — instead of guessing an
extension or relying on shell=True (which also avoids any shell-quoting
concerns for arguments that come from user input, e.g. PR titles).
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass

AZ_INSTALL_HINT = "Install the Azure CLI: https://learn.microsoft.com/cli/azure/install-azure-cli"
GH_INSTALL_HINT = "Install the GitHub CLI: https://cli.github.com"
PSQL_INSTALL_HINT = "Install the PostgreSQL client tools (psql): https://www.postgresql.org/download/"

# `pr status --wait` exits with this code (not 0=success, not 1=CI failure) when it stops
# because a build/check needs a human to approve it — there's nothing more the CLI can do
# but wait indefinitely, which defeats the point of --wait. Deliberately not 2: that's
# Click/Typer's own exit code for a CLI usage error (e.g. typer.BadParameter elsewhere in
# this tool), and callers branching on exit code shouldn't confuse the two.
EXIT_NEEDS_APPROVAL = 3

# How long a `--wait` poll loop may go without printing anything before `PollHeartbeat`
# forces a line out anyway. Every `--wait` loop in this tool polls every 30s but used to
# print only when the status text changed -- for a check/build that sits in the same
# state for a long CI run, that meant long stretches of complete silence, indistinguishable
# from a hang. 10 minutes is short enough to reassure someone watching it live, long enough
# not to spam a captured log.
POLL_HEARTBEAT_SECS = 600.0

# Bounds a single call to an external CLI (`gh`/`az`/`git`) or a single Azure DevOps REST
# request. Without a timeout, a stalled network call or the CLI blocking on an interactive
# prompt (e.g. an expired `az`/`gh` login) could hang a command -- most importantly a
# `--wait` poll loop -- forever with no way to know why. 30s is generous for any single
# call these commands make (none of them are large/slow by nature) while still bounding
# the wait to something a human would notice and investigate.
CLI_TIMEOUT_SECS = 30.0
HTTP_TIMEOUT_SECS = 30.0


class PollHeartbeat:
    """Tracks a repeatedly-polled status line for a `--wait` loop and decides when to
    (re)print it: on every change, as before, plus at least once every `heartbeat_secs`
    even when the status hasn't changed -- so the loop can never go silent long enough to
    look stuck.

    An unchanged status reprints as a fresh, timestamped line rather than the plain
    `\\r`-prefixed overwrite a real change gets: overwriting the same line in place would
    be invisible on both a live terminal (nothing looks different) and a captured log
    (the `\\r` produces no new line at all), defeating the point of a heartbeat.
    """

    def __init__(self, heartbeat_secs: float = POLL_HEARTBEAT_SECS) -> None:
        self._heartbeat_secs = heartbeat_secs
        self._last_line = ""
        self._last_print: float | None = None

    def show(self, msg: str) -> None:
        now = time.monotonic()
        if msg == self._last_line and self._last_print is not None and now - self._last_print < self._heartbeat_secs:
            return
        if msg != self._last_line:
            print(msg, end="", flush=True)
        else:
            print(f"\n[{time.strftime('%H:%M:%S')}] still waiting: {msg.lstrip(chr(13))}", flush=True)
        self._last_line = msg
        self._last_print = now


def is_claude_code() -> bool:
    """True if this process is running as a subprocess of Claude Code.

    Claude Code sets `CLAUDECODE=1` on every subprocess it spawns — confirmed
    empirically, not a documented/stable contract, so treat this as best-effort
    (spoofable, and could change in a future Claude Code release).
    """
    return os.environ.get("CLAUDECODE") == "1"


@dataclass(frozen=True)
class AgentSession:
    """A detected coding agent plus a session identifier worth recording for traceability."""

    name: str
    session_id: str


def _claude_session_id() -> str | None:
    """A Claude Code session identifier, preferring a browsable link.

    `CLAUDE_CODE_BRIDGE_SESSION_ID` is only set when the session is bridged to
    claude.ai (e.g. remote/cloud sessions) -- that's what turns into the
    `https://claude.ai/code/<id>` link a human can actually open.
    `CLAUDE_CODE_SESSION_ID` (a local uuid, no hosted page) is the fallback for a
    plain local CLI session, so there's still *something* recorded either way.
    """
    bridge_id = os.environ.get("CLAUDE_CODE_BRIDGE_SESSION_ID")
    if bridge_id:
        return f"https://claude.ai/code/{bridge_id}"
    return os.environ.get("CLAUDE_CODE_SESSION_ID")


# Tool-specific env var(s) known to carry a session id, keyed by the substring
# that would appear in AI_AGENT's value (see `detect_agent_session`) for that
# tool. Tried in order; first one present wins. Codex CLI and Gemini CLI mark
# their subprocesses too (CODEX_SANDBOX*/GEMINI_CLI=1) but don't currently
# expose a session id via any documented env var, so there's nothing
# distinguishing to look up for them -- no entry needed, they just fall
# through to recording the raw AI_AGENT tag instead.
_SESSION_ID_ENV_VARS: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"),
    "goose": ("GOOSE_SESSION",),
    "opencode": ("OPENCODE_SESSION_ID",),
}

# Display names for the AI_AGENT tags above (and other known agents) that a plain
# `tag.title()` wouldn't get right -- e.g. "opencode" -> "OpenCode", not "Opencode".
_AGENT_DISPLAY_NAMES = {
    "claude": "Claude",
    "goose": "Goose",
    "opencode": "OpenCode",
    "codex": "Codex",
    "gemini": "Gemini",
}


def detect_agent_session() -> AgentSession | None:
    """Best-effort: which coding agent (if any) `bdt` is running under, plus a session
    identifier for it -- so PR/issue content bdt creates can be traced back to the
    session that made it. Returns None when no agent is detected, or none of the
    lookups below found an actual identifier for it; callers must treat that as
    "nothing to record", never as an error.

    `AI_AGENT` is the agent-agnostic signal here: `gh` itself reads this env var (any
    AI coding tool can set it to self-identify, for GitHub-side attribution), so any
    tool following that convention is picked up here without this function needing a
    hardcoded check per tool. Claude Code sets it to something like
    "claude-code_2-1-270_agent" -- CLAUDECODE=1 is checked first as a decisive,
    Claude-specific signal so Claude still gets its richer session link (see
    `_claude_session_id`) even if AI_AGENT's format or value ever changes.
    """
    if os.environ.get("CLAUDECODE") == "1":
        session_id = _claude_session_id()
        if session_id:
            return AgentSession("Claude", session_id)

    ai_agent = os.environ.get("AI_AGENT", "").strip()
    if ai_agent:
        tag = ai_agent.split("_")[0].lower()
        name = _AGENT_DISPLAY_NAMES.get(tag, tag.replace("-", " ").title())
        for known_tag, env_vars in _SESSION_ID_ENV_VARS.items():
            if known_tag in tag:
                for var in env_vars:
                    value = os.environ.get(var)
                    if value:
                        return AgentSession(name, value)
                break
        return AgentSession(name, ai_agent)

    # No AI_AGENT (a tool that doesn't follow that convention, or an older
    # version of one that does) -- fall back to each agent's own presence
    # signal, for the ones known to also expose a session id this way.
    goose_session = os.environ.get("GOOSE_SESSION")
    if goose_session:
        return AgentSession("Goose", goose_session)
    if os.environ.get("OPENCODE"):
        opencode_session = os.environ.get("OPENCODE_SESSION_ID")
        if opencode_session:
            return AgentSession("OpenCode", opencode_session)
    return None


def ensure_agent_session_note(text: str | None, *, also_check: str | None = None) -> str | None:
    """`text` with a trailing `"<Agent> Session: <id>"` line appended, if bdt is running
    under a detected coding agent (see `detect_agent_session`) and neither `text` nor
    `also_check` (e.g. a title, checked but never modified) already mentions that
    session. Returns `text` unchanged when no agent is detected or the session is
    already referenced -- including unchanged `None`, so callers for whom `None`
    means "leave this field alone" (e.g. an update that didn't touch it) keep that
    meaning.
    """
    session = detect_agent_session()
    if session is None:
        return text
    if session.session_id in (text or "") or (also_check and session.session_id in also_check):
        return text
    note = f"{session.name} Session: {session.session_id}"
    body = text or ""
    return f"{body}\n\n{note}" if body.strip() else note


def require_tool(name: str, install_hint: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"'{name}' is required for this command but wasn't found on PATH.\n{install_hint}")
    return path


def require_az() -> str:
    return require_tool("az", AZ_INSTALL_HINT)


def require_gh() -> str:
    return require_tool("gh", GH_INSTALL_HINT)


def require_psql() -> str:
    return require_tool("psql", PSQL_INSTALL_HINT)
