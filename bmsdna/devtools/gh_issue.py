"""GitHub issue create/update/delete and comment add/update/delete via the `gh` CLI.

Screenshot/file support reuses the `pr-assets` orphan-branch trick from
`gh_pr.py` (GitHub has no API for uploading a file straight into an issue
body/comment — only the web UI's drag-and-drop). Each issue gets its own
`issue-<number>` folder there, mirroring how `gh_pr.py` uses one folder per
source branch.

`gh issue` has no subcommand for editing/deleting a single comment by ID
(only `gh issue comment --edit-last`, which only touches your own most
recent comment) — those two go through `gh api` directly against GitHub's
REST API instead: `PATCH`/`DELETE /repos/{owner}/{repo}/issues/comments/{comment_id}`
(issue and PR conversation comments share this same endpoint namespace,
addressed by comment_id alone — no issue number needed in the URL, though
the CLI still asks for one so `bdt issue comment` reads the same for both
hosts).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from .bdt_config import load_bdt_table
from .gh_pr import push_assets
from .pr_markdown import build_attachments_section, build_comment_content, build_screenshots_section

_COMMENT_ID_RE = re.compile(r"#issuecomment-(\d+)")

# Raw pool size to fetch from `gh issue list` before filtering to a board, as a multiple of the
# caller's requested `limit` -- board membership is filtered client-side after the fact (`gh issue
# list` has no board-scoping of its own), so asking for exactly `limit` up front would usually
# return fewer than that once off-board results are dropped.
_BOARD_SEARCH_OVERFETCH = 10

# Max items to pull back per board when resolving its membership -- generous enough to cover any
# board this is likely to be pointed at without needing pagination.
_BOARD_ITEM_LIMIT = 500


def _run_gh(gh: str, args: list[str]) -> str:
    r = subprocess.run([gh, *args], capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or f"`gh {' '.join(args)}` failed")
    return r.stdout.strip()


def _screenshot_images(owner: str, repo: str, key: str, screenshot_paths: list[str]) -> list[tuple[str, str]]:
    urls = push_assets(owner, repo, key, screenshot_paths)
    return list(zip((Path(p).name for p in screenshot_paths), urls))


def _file_links(owner: str, repo: str, key: str, file_paths: list[str]) -> list[tuple[str, str]]:
    urls = push_assets(owner, repo, key, file_paths)
    return list(zip((Path(p).name for p in file_paths), urls))


def parse_issue_number(issue_url: str) -> str:
    """Pull the issue number off the URL `gh issue create` prints, e.g.
    'https://github.com/owner/repo/issues/42' -> '42'.
    """
    return issue_url.rstrip("/").rsplit("/", 1)[-1]


def parse_comment_id(comment_url: str) -> str | None:
    """Pull the numeric comment id off a comment URL's '#issuecomment-<id>' fragment."""
    match = _COMMENT_ID_RE.search(comment_url)
    return match.group(1) if match else None


def resolve_board(board: str | None, start: Path | None = None) -> str | None:
    """--board wins; else `[tool.bdt.github].board` from pyproject.toml; else None."""
    return board or load_bdt_table("github", start).get("board")


def _find_project_number(projects: list[dict], board: str) -> int | None:
    for project in projects:
        if project.get("title", "").casefold() == board.casefold():
            return project.get("number")
    return None


def resolve_project_number(gh: str, owner: str, board: str) -> int:
    """The GitHub Projects (v2) board `board` (a number or a title) as its project number."""
    if board.isdigit():
        return int(board)
    out = _run_gh(gh, ["project", "list", "--owner", owner, "--format", "json", "--closed", "--limit", str(_BOARD_ITEM_LIMIT)])
    projects = json.loads(out).get("projects", []) if out else []
    number = _find_project_number(projects, board)
    if number is None:
        sys.exit(f"GitHub Project board '{board}' not found for owner '{owner}'. Pass its number instead, or check the name.")
    return number


_ISSUE_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")


def _extract_issue_numbers(items: list[dict], owner: str, repo: str) -> set[int]:
    """Issue numbers among a board's items that belong to `owner/repo` -- drops pull requests and
    draft issues (no repo issue number to match against), and issues from other repos on the same
    org-wide board: issue numbers are only unique within a single repo, so matching on the bare
    number alone would treat e.g. some-other-repo#7 as this repo's #7 too.
    """
    numbers = set()
    for item in items:
        if item.get("type") != "Issue":
            continue
        url = item.get("url")
        if not url:
            continue
        match = _ISSUE_URL_RE.search(url)
        if match and match.group(1).casefold() == owner.casefold() and match.group(2).casefold() == repo.casefold():
            numbers.add(int(match.group(3)))
    return numbers


def board_issue_numbers(gh: str, owner: str, repo: str, project_number: int) -> set[int]:
    """Issue numbers on `owner`'s Projects (v2) board `project_number` that belong to `owner/repo`."""
    out = _run_gh(gh, ["project", "item-list", str(project_number), "--owner", owner, "--format", "json", "--limit", str(_BOARD_ITEM_LIMIT)])
    items = json.loads(out).get("items", []) if out else []
    return _extract_issue_numbers(items, owner, repo)


def create(
    gh: str,
    owner: str,
    repo: str,
    title: str,
    body: str | None,
    labels: list[str],
    screenshot_paths: list[str],
    extra_args: list[str],
    file_paths: list[str] | None = None,
    board: str | None = None,
) -> None:
    """Create a GitHub issue, then (if any) attach screenshots/files as a follow-up edit.

    `board` adds the issue to that GitHub Projects (v2) board by title (`gh issue create
    --project` takes a name, not a number -- unlike board-scoped search, which needs the number
    to query the board's items).
    """
    file_paths = file_paths or []
    args = ["issue", "create", "--title", title, "--body", body or ""]
    for label in labels:
        args += ["--label", label]
    if board:
        args += ["--project", board]
    args += extra_args

    url = _run_gh(gh, args)
    print(url)
    number = parse_issue_number(url)

    if screenshot_paths or file_paths:
        new_body: str = body or ""
        if screenshot_paths:
            new_body = build_screenshots_section(new_body, _screenshot_images(owner, repo, f"issue-{number}", screenshot_paths))
        if file_paths:
            new_body = build_attachments_section(new_body, _file_links(owner, repo, f"issue-{number}", file_paths))
        _run_gh(gh, ["issue", "edit", number, "--body", new_body])
        print(f"Attached {len(screenshot_paths)} screenshot(s), {len(file_paths)} file(s) to issue #{number}")


def build_search_query(keywords: list[str], since: str | None) -> str:
    """The `gh issue list --search` query string: keywords ANDed together (GitHub search's
    implicit default), optionally scoped to issues updated on/after `since` (an ISO
    'YYYY-MM-DD' date) via the `updated:` qualifier. Always sorted `updated:desc` — GitHub's
    default search order is text-relevance, not recency, which `search()`'s ordering relies on.
    `keywords` may be empty, to list issues without a text filter.
    """
    parts = [*keywords, "sort:updated-desc"]
    if since:
        parts.append(f"updated:>={since}")
    return " ".join(parts)


def search(
    gh: str, owner: str, repo: str, keywords: list[str], since: str | None, limit: int, state: str = "open", board: str | None = None
) -> list[dict]:
    """Search (or, with no keywords, just list) issues by state, most recently updated first.

    `state` is `gh issue list`'s own `open|closed|all` flag, not a search qualifier. `board`
    scopes results to a GitHub Projects (v2) board (by title or number) -- `gh issue list` has no
    board filter of its own, so this over-fetches a wider raw pool first and filters down to
    `limit` afterward, client-side.
    """
    query = build_search_query(keywords, since)
    fetch_limit = limit * _BOARD_SEARCH_OVERFETCH if board else limit
    out = _run_gh(gh, ["issue", "list", "--search", query, "--state", state, "--limit", str(fetch_limit), "--json", "number,title,url,state"])
    items = json.loads(out) if out else []

    if board:
        project_number = resolve_project_number(gh, owner, board)
        allowed = board_issue_numbers(gh, owner, repo, project_number)
        items = [item for item in items if item["number"] in allowed]

    items = items[:limit]
    for item in items:
        print(f"#{item['number']} [{item['state']}] {item['title']}")
        print(item["url"])
    if not items:
        print("No matching issues found.")
    return items


_DONE_STATE_NAMES = ("closed", "done", "completed")
_REMOVED_STATE_NAMES = ("removed", "not planned", "not_planned", "wontfix", "won't fix")
_OPEN_STATE_NAMES = ("open", "reopened", "reopen")


def _set_state(gh: str, number: int, state: str) -> None:
    """GitHub issues only have two states (open/closed) plus, when closed, a `state_reason` of
    'completed' or 'not planned' — no per-process-template state names like Azure DevOps. Map the
    common terminal-state spellings onto that: 'Closed'/'Done'/'Completed' close as completed
    (GitHub's "done" concept); 'Removed'/'Not Planned'/'Wontfix' close as not planned; 'Open'/
    'Reopened' reopens. Anything else has no GitHub equivalent — leave the issue's state
    unchanged and comment with the exact state that was requested, so it isn't silently dropped.
    """
    normalized = state.strip().lower()
    if normalized in _DONE_STATE_NAMES:
        _run_gh(gh, ["issue", "close", str(number), "--reason", "completed"])
    elif normalized in _REMOVED_STATE_NAMES:
        _run_gh(gh, ["issue", "close", str(number), "--reason", "not planned"])
    elif normalized in _OPEN_STATE_NAMES:
        _run_gh(gh, ["issue", "reopen", str(number)])
    else:
        _run_gh(
            gh,
            [
                "issue",
                "comment",
                str(number),
                "--body",
                f"Requested state change to '{state}', which isn't a valid GitHub issue state — left unchanged.",
            ],
        )


def update(
    gh: str,
    number: int,
    title: str | None = None,
    body: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    state: str | None = None,
) -> None:
    args = ["issue", "edit", str(number)]
    if title is not None:
        args += ["--title", title]
    if body is not None:
        args += ["--body", body]
    for label in add_labels or []:
        args += ["--add-label", label]
    for label in remove_labels or []:
        args += ["--remove-label", label]
    if len(args) == 3 and state is None:
        sys.exit("Nothing to update — provide at least one of --title, --description, --label, --remove-label, --state.")

    if len(args) > 3:
        _run_gh(gh, args)
    if state is not None:
        _set_state(gh, number, state)
    print(f"Updated issue #{number}")


def delete(gh: str, number: int) -> None:
    """Permanently deletes the issue — unlike Azure DevOps work items, GitHub has no recycle bin for issues."""
    _run_gh(gh, ["issue", "delete", str(number), "--yes"])
    print(f"Deleted issue #{number}")


def comment(
    gh: str,
    owner: str,
    repo: str,
    number: int,
    message: str | None,
    screenshot_paths: list[str],
    file_paths: list[str] | None = None,
) -> None:
    """Post a comment, with a message and/or screenshots/files, on a GitHub issue."""
    file_paths = file_paths or []
    images = _screenshot_images(owner, repo, f"issue-{number}", screenshot_paths) if screenshot_paths else []
    files = _file_links(owner, repo, f"issue-{number}", file_paths) if file_paths else []
    content = build_comment_content(message, images, files)
    url = _run_gh(gh, ["issue", "comment", str(number), "--body", content])
    comment_id = parse_comment_id(url)
    suffix = f" (comment #{comment_id})" if comment_id else ""
    print(f"Added comment ({len(screenshot_paths)} screenshot(s), {len(file_paths)} file(s)) to issue #{number}{suffix}")
    print(url)


def update_comment(gh: str, owner: str, repo: str, comment_id: str, text: str) -> None:
    _run_gh(gh, ["api", "--method", "PATCH", f"repos/{owner}/{repo}/issues/comments/{comment_id}", "-f", f"body={text}"])
    print(f"Updated comment #{comment_id}")


def delete_comment(gh: str, owner: str, repo: str, comment_id: str) -> None:
    _run_gh(gh, ["api", "--method", "DELETE", f"repos/{owner}/{repo}/issues/comments/{comment_id}"])
    print(f"Deleted comment #{comment_id}")
