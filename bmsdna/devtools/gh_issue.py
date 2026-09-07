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

import re
import subprocess
import sys
from pathlib import Path

from .gh_pr import push_screenshots
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


def update(
    gh: str,
    number: int,
    title: str | None = None,
    body: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
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
    if len(args) == 3:
        sys.exit("Nothing to update — provide at least one of --title, --description, --label, --remove-label.")

    _run_gh(gh, args)
    print(f"Updated issue #{number}")


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
