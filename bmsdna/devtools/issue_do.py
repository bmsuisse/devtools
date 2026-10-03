"""`bdt issue do <number>`: hand an issue / work item to a coding agent.

Fetches the issue's title and description (GitHub via `gh`, Azure DevOps via the REST API)
and starts the agent headless (`claude -p`), naming the session `<number>: <title>` so it is
easy to find again with `claude --resume`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys

import requests

from .ado_issue import _base_url
from .gitrepo import AdoRemote

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def fetch_github(gh: str, number: int) -> tuple[str, str]:
    result = subprocess.run([gh, "issue", "view", str(number), "--json", "title,body"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"gh issue view {number} failed:\n{result.stderr.strip()}")
    data = json.loads(result.stdout)
    return data["title"], data.get("body") or ""


def fetch_ado(session: requests.Session, remote: AdoRemote, number: int) -> tuple[str, str]:
    r = session.get(
        f"{_base_url(remote)}/_apis/wit/workitems/{number}",
        params={"fields": "System.Title,System.Description", "api-version": "7.1"},
    )
    r.raise_for_status()
    fields = r.json()["fields"]
    # ADO descriptions are HTML; the agent only needs the text.
    description = _HTML_TAG_RE.sub("", (fields.get("System.Description") or "").replace("<br>", "\n").replace("</div>", "\n"))
    return fields["System.Title"], description.strip()


def session_name(number: int, title: str) -> str:
    return f"{number}: {title}"


def build_prompt(number: int, title: str, body: str) -> str:
    return (
        f"Work on issue {number}: {title}\n\n{body.strip() or '(no description)'}\n\n"
        "Follow the dev-workflow skill: worktree, draft PR early, implement, test, publish the PR "
        f"and reference issue {number} in it. Do not merge. `bdt issue do` has already marked "
        f"issue {number} as taken (with this session), so don't run `bdt issue take`."
    )


def take_message(user: str, agent: str, session_id: str | None) -> str:
    """The "Taken by" comment `bdt issue do` posts before the agent starts. It names the session itself,
    because the agent's own session doesn't exist yet and `bdt`'s auto-detected one (if `bdt issue do` is
    run from inside an agent) would be the wrong one."""
    if session_id is None:
        return f"Taken by {user} (via {agent})"
    return f"Taken by {user}\n\nClaude Session: {session_id} (resume with `claude --resume {session_id}`)"


def build_command(agent: str, number: int, title: str, body: str, extra: list[str], session_id: str | None = None) -> list[str]:
    prompt = build_prompt(number, title, body)
    if agent == "claude":
        session = ["--session-id", session_id] if session_id else []
        return [agent, "-p", prompt, "--name", session_name(number, title), *session, *extra]
    # Other agents: no known naming flag, so just hand over the prompt.
    return [agent, *extra, prompt]


def run(number: int, title: str, body: str, *, agent: str, extra: list[str], dry_run: bool, session_id: str | None = None) -> None:
    cmd = build_command(agent, number, title, body, extra, session_id)
    if dry_run:
        print(" ".join(json.dumps(c) if " " in c or "\n" in c else c for c in cmd))
        return
    if shutil.which(agent) is None:
        sys.exit(f"'{agent}' is required for this command but wasn't found on PATH.")
    raise SystemExit(subprocess.run(cmd, check=False).returncode)
