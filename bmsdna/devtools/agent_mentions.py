"""Which coding agent is calling `bdt`, and does a comment address it?

Backs `bdt issue search --for-me` and `bdt issue watch`: people (or other agents) address an agent
in an issue comment with `@Claude`, `@Codex`, `@Goose`, ... and `@Agent` addresses all of them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping

# A mention addressed to every agent, whatever the caller is.
ANY_AGENT = "agent"

# Env var that overrides detection (the `--agent` option of `issue search`/`issue watch` reads it too).
AGENT_ENV_VAR = "BDT_AGENT"

# Mention handles of the agents we know how to detect, with the environment each one leaves in the
# processes it spawns. Best-effort, like `cli_tools.detect_agent_session`: none of these is a
# documented, stable contract, which is why an explicit override always wins.
_ENV_EXACT: tuple[tuple[str, str, str], ...] = (("CLAUDECODE", "1", "claude"), ("GEMINI_CLI", "1", "gemini"))
_ENV_PREFIXES: tuple[tuple[str, str], ...] = (
    ("CLAUDE_CODE_", "claude"),
    ("CODEX_SANDBOX", "codex"),
    ("CODEX_THREAD_", "codex"),
    ("CODEX_CI", "codex"),
    ("GOOSE_TERMINAL", "goose"),
    ("GOOSE_SESSION", "goose"),
    ("OPENCODE", "opencode"),
)


def normalize_agent(name: str) -> str:
    """The mention handle for an agent name or tag: 'Claude' / 'claude-code_2-1_agent' -> 'claude',
    'Codex CLI' -> 'codex', '@Goose' -> 'goose'."""
    tag = re.split(r"[\s_-]+", name.strip().lstrip("@").lower(), maxsplit=1)[0]
    return tag


def detect_agent(env: Mapping[str, str] | None = None, override: str | None = None) -> str | None:
    """The calling agent's mention handle (lowercase, e.g. 'claude'), or None when not run by one.

    Order: an explicit `override` (the `--agent` option), then `BDT_AGENT`, then `CLAUDECODE=1`,
    then the agent-agnostic `AI_AGENT` convention (the first token of its value; `gh` reads the same
    variable), then each agent's own marker variables (`CLAUDE_CODE_*`, `CODEX_*`, `GOOSE_*`, ...).
    """
    env = os.environ if env is None else env
    for explicit in (override, env.get(AGENT_ENV_VAR)):
        if explicit and explicit.strip():
            return normalize_agent(explicit)
    for var, value, handle in _ENV_EXACT:
        if env.get(var) == value:
            return handle
    ai_agent = env.get("AI_AGENT", "").strip()
    if ai_agent:
        return normalize_agent(ai_agent)
    for prefix, handle in _ENV_PREFIXES:
        if any(key.startswith(prefix) and env[key] for key in env):
            return handle
    return None


def mentions_agent(text: str | None, agent: str | None) -> bool:
    """Does `text` contain an `@<agent>` or `@Agent` mention?

    Case-insensitive, and the handle must be a whole word: `@Claude` and `@claude,` match but
    `@Claudette`, `@claude-bot` and `name@claude.com` do not. With `agent=None` (no agent detected)
    only `@Agent` matches.
    """
    if not text:
        return False
    handles = {ANY_AGENT}
    if agent:
        handles.add(normalize_agent(agent))
    pattern = r"(?<![\w@])@(?:" + "|".join(re.escape(h) for h in sorted(handles)) + r")(?![\w@]|-\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def last_comment_filter(agent: str | None) -> Callable[[str | None], bool]:
    """A predicate over an issue's newest comment (None = no comments): does it address `agent`?"""
    return lambda text: mentions_agent(text, agent)
