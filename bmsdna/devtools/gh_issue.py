"""GitHub issue create/update/delete and comment add/update/delete via the `gh` CLI.

Screenshot support reuses the `pr-assets` orphan-branch trick from
`gh_pr.py` (GitHub has no API for uploading an image straight into an issue
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

from .gh_pr import push_screenshots
from .issue_state import DONE_STATE_NAMES, OPEN_STATE_NAMES, REMOVED_STATE_NAMES
from .pr_markdown import build_screenshots_section

_COMMENT_ID_RE = re.compile(r"#issuecomment-(\d+)")


def _run_gh(gh: str, args: list[str]) -> str:
    r = subprocess.run([gh, *args], capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or f"`gh {' '.join(args)}` failed")
    return r.stdout.strip()


def _screenshot_images(owner: str, repo: str, key: str, screenshot_paths: list[str]) -> list[tuple[str, str]]:
    urls = push_screenshots(owner, repo, key, screenshot_paths)
    return list(zip((Path(p).name for p in screenshot_paths), urls))


def parse_issue_number(issue_url: str) -> str:
    """Pull the issue number off the URL `gh issue create` prints, e.g.
    'https://github.com/owner/repo/issues/42' -> '42'.
    """
    return issue_url.rstrip("/").rsplit("/", 1)[-1]


def parse_comment_id(comment_url: str) -> str | None:
    """Pull the numeric comment id off a comment URL's '#issuecomment-<id>' fragment."""
    match = _COMMENT_ID_RE.search(comment_url)
    return match.group(1) if match else None


def create(
    gh: str,
    owner: str,
    repo: str,
    title: str,
    body: str | None,
    labels: list[str],
    screenshot_paths: list[str],
    extra_args: list[str],
) -> None:
    """Create a GitHub issue, then (if any) attach screenshots as a follow-up edit."""
    args = ["issue", "create", "--title", title, "--body", body or ""]
    for label in labels:
        args += ["--label", label]
    args += extra_args

    url = _run_gh(gh, args)
    print(url)
    number = parse_issue_number(url)

    if screenshot_paths:
        images = _screenshot_images(owner, repo, f"issue-{number}", screenshot_paths)
        new_body = build_screenshots_section(body, images)
        _run_gh(gh, ["issue", "edit", number, "--body", new_body])
        print(f"Attached {len(screenshot_paths)} screenshot(s) to issue #{number}")


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


def search(gh: str, keywords: list[str], since: str | None, limit: int, state: str = "open") -> list[dict]:
    """Search (or, with no keywords, just list) issues by state, most recently updated first.

    `state` is `gh issue list`'s own `open|closed|all` flag, not a search qualifier.
    """
    query = build_search_query(keywords, since)
    out = _run_gh(gh, ["issue", "list", "--search", query, "--state", state, "--limit", str(limit), "--json", "number,title,url,state"])
    items = json.loads(out) if out else []
    for item in items:
        print(f"#{item['number']} [{item['state']}] {item['title']}")
        print(item["url"])
    if not items:
        print("No matching issues found.")
    return items


def _set_state(gh: str, number: int, state: str) -> bool:
    """GitHub issues only have two states (open/closed) plus, when closed, a `state_reason` of
    'completed' or 'not planned' — no per-process-template state names like Azure DevOps. Map the
    common terminal-state spellings onto that: 'Closed'/'Done'/'Completed' close as completed
    (GitHub's "done" concept); 'Removed'/'Not Planned'/'Wontfix' close as not planned; 'Open'/
    'Reopened' reopens. Anything else has no GitHub equivalent — leave the issue's state
    unchanged and comment with the exact state that was requested, so it isn't silently dropped.

    Returns whether the issue's state was actually changed (False when only an explanatory
    comment was posted), so callers don't report a successful update that didn't happen.
    """
    normalized = state.strip().lower()
    if normalized in DONE_STATE_NAMES:
        _run_gh(gh, ["issue", "close", str(number), "--reason", "completed"])
        return True
    elif normalized in REMOVED_STATE_NAMES:
        _run_gh(gh, ["issue", "close", str(number), "--reason", "not planned"])
        return True
    elif normalized in OPEN_STATE_NAMES:
        _run_gh(gh, ["issue", "reopen", str(number)])
        return True
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
        return False


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

    edited_other_fields = len(args) > 3
    if edited_other_fields:
        _run_gh(gh, args)

    state_applied = True
    if state is not None:
        state_applied = _set_state(gh, number, state)

    if edited_other_fields or state_applied:
        print(f"Updated issue #{number}")
    else:
        print(f"Issue #{number}: '{state}' isn't a valid GitHub issue state — noted in a comment.")


def delete(gh: str, number: int) -> None:
    """Permanently deletes the issue — unlike Azure DevOps work items, GitHub has no recycle bin for issues."""
    _run_gh(gh, ["issue", "delete", str(number), "--yes"])
    print(f"Deleted issue #{number}")


def comment(gh: str, owner: str, repo: str, number: int, message: str | None, screenshot_paths: list[str]) -> None:
    """Post a comment, with a message and/or screenshots, on a GitHub issue."""
    images = _screenshot_images(owner, repo, f"issue-{number}", screenshot_paths) if screenshot_paths else []
    content = build_screenshots_section(message, images).strip() if images else (message or "")
    url = _run_gh(gh, ["issue", "comment", str(number), "--body", content])
    comment_id = parse_comment_id(url)
    suffix = f" (comment #{comment_id})" if comment_id else ""
    print(f"Added comment ({len(screenshot_paths)} screenshot(s)) to issue #{number}{suffix}")
    print(url)


def update_comment(gh: str, owner: str, repo: str, comment_id: str, text: str) -> None:
    _run_gh(gh, ["api", "--method", "PATCH", f"repos/{owner}/{repo}/issues/comments/{comment_id}", "-f", f"body={text}"])
    print(f"Updated comment #{comment_id}")


def delete_comment(gh: str, owner: str, repo: str, comment_id: str) -> None:
    _run_gh(gh, ["api", "--method", "DELETE", f"repos/{owner}/{repo}/issues/comments/{comment_id}"])
    print(f"Deleted comment #{comment_id}")
