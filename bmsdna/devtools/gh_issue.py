"""GitHub issue creation/comment via the `gh` CLI.

Screenshot support reuses the `pr-assets` orphan-branch trick from
`gh_pr.py` (GitHub has no API for uploading an image straight into an issue
body/comment — only the web UI's drag-and-drop). Each issue gets its own
`issue-<number>` folder there, mirroring how `gh_pr.py` uses one folder per
source branch.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .gh_pr import push_screenshots
from .pr_markdown import build_screenshots_section


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


def comment(gh: str, owner: str, repo: str, number: int, message: str | None, screenshot_paths: list[str]) -> None:
    """Post a comment, with a message and/or screenshots, on a GitHub issue."""
    images = _screenshot_images(owner, repo, f"issue-{number}", screenshot_paths) if screenshot_paths else []
    content = build_screenshots_section(message, images).strip() if images else (message or "")
    _run_gh(gh, ["issue", "comment", str(number), "--body", content])
    print(f"Added comment ({len(screenshot_paths)} screenshot(s)) to issue #{number}")
