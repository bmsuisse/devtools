"""GitHub PR merge/check status and creation via the `gh` CLI.

Deliberately avoids `gh pr checks --json` — that flag was only added in a
later `gh` release than some machines still run (confirmed missing on gh
2.45.0). Everything here is built on `gh pr view --json ...`, whose --json
support has been stable for a long time, plus statusCheckRollup entries
categorized ourselves using GitHub's documented GraphQL enums.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .pr_markdown import build_screenshots_section

PR_VIEW_FIELDS = "number,title,baseRefName,mergeable,statusCheckRollup"

# GitHub has no API for uploading images to a PR description (only the web
# UI's drag-and-drop, which needs a browser session). The standard
# workaround: keep screenshots on their own orphan branch, one folder per
# source branch, and link to them with a raw blob URL.
SCREENSHOTS_BRANCH = "pr-assets"

# PullRequest.mergeable (GraphQL MergeableState).
CONFLICTING = "CONFLICTING"
UNKNOWN_MERGEABLE = "UNKNOWN"

# statusCheckRollup entries are a union of CheckRun | StatusContext.
# CheckRun.conclusion (GraphQL CheckConclusionState) -> bucket.
_CHECK_RUN_BUCKET = {
    "SUCCESS": "pass",
    "NEUTRAL": "pass",
    "SKIPPED": "skipping",
    "CANCELLED": "cancel",
    "FAILURE": "fail",
    "TIMED_OUT": "fail",
    "ACTION_REQUIRED": "fail",
    "STALE": "fail",
}
# Legacy commit Status.state (GraphQL StatusState) -> bucket.
_STATUS_CONTEXT_BUCKET = {
    "SUCCESS": "pass",
    "PENDING": "pending",
    "EXPECTED": "pending",
    "ERROR": "fail",
    "FAILURE": "fail",
}


def _run_gh_json(gh: str, args: list[str]) -> dict:
    r = subprocess.run([gh, *args], capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or f"`gh {' '.join(args)}` failed")
    return json.loads(r.stdout)


def get_pr(gh: str) -> dict:
    """The PR for the current branch, however `gh` resolves it — there's no
    target-branch filter on `gh pr view` the way ADO's search API has one.
    """
    return _run_gh_json(gh, ["pr", "view", "--json", PR_VIEW_FIELDS])


def check_bucket(check: dict) -> str:
    if check.get("__typename") == "StatusContext":
        return _STATUS_CONTEXT_BUCKET.get(check.get("state"), "pending")
    if check.get("status") != "COMPLETED":
        return "pending"
    return _CHECK_RUN_BUCKET.get(check.get("conclusion"), "fail")


def check_label(check: dict) -> str:
    name = check.get("name", "?")
    workflow = check.get("workflowName")
    return f"{workflow} / {name}" if workflow and workflow not in name else name


def merge_conflict_message(pr: dict) -> str | None:
    if pr.get("mergeable") != CONFLICTING:
        return None
    return f"PR #{pr.get('number')} ({pr.get('title', '?')!r}) has merge conflicts with '{pr.get('baseRefName', '?')}' (mergeable=CONFLICTING)"


def print_check(check: dict) -> None:
    bucket = check_bucket(check)
    icon = {"pass": "✓", "fail": "✗", "cancel": "⊘"}.get(bucket, "…")
    print(f"  [{icon} {bucket.upper()}] {check_label(check)}")


def run(gh: str, wait: bool) -> None:
    last_line = ""
    while True:
        pr = get_pr(gh)

        # GitHub hasn't finished computing mergeability yet (usually resolves
        # within a couple seconds); worth a short wait even outside --wait mode
        # isn't safe (could spin forever if it never resolves) — only retry
        # when the caller already opted into waiting.
        if pr.get("mergeable") == UNKNOWN_MERGEABLE and wait:
            time.sleep(3)
            continue

        conflict = merge_conflict_message(pr)
        if conflict:
            sys.exit(conflict)

        pr_number = pr.get("number")
        title = pr.get("title", "?")
        base = pr.get("baseRefName", "?")
        msg = f"\rPR #{pr_number}: {title} (base={base})"

        checks = pr.get("statusCheckRollup") or []
        if not checks:
            print(msg + " | no checks found.")
            return

        buckets = [check_bucket(c) for c in checks]
        msg += " | " + ", ".join(f"{check_label(c)}: {check_bucket(c)}" for c in checks)

        if "pending" in buckets and wait:
            if msg != last_line:
                print(msg, end="", flush=True)
                last_line = msg
            time.sleep(30)
            continue

        print(msg)
        print("\nDetails:")
        for c in checks:
            print_check(c)

        if "fail" in buckets:
            sys.exit(1)
        return


def create(gh: str, target: str, extra_args: list[str]) -> int:
    """Create a GitHub PR from the current branch into `target`.

    --fill autofills title/body from commit info so this never blocks on an
    interactive prompt; pass --title/--body in extra_args to override (gh
    lets explicit values take precedence over --fill).
    """
    cmd = [gh, "pr", "create", "--base", target, "--fill", *extra_args]
    return subprocess.run(cmd).returncode


def _git(args: list[str], env: dict[str, str] | None = None) -> str:
    r = subprocess.run(["git", *args], capture_output=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or f"`git {' '.join(args)}` failed")
    return r.stdout.strip()


def push_screenshots(owner: str, repo: str, branch: str, paths: list[str], max_attempts: int = 5) -> list[str]:
    """Push `paths` to a `<branch>/` folder on the `pr-assets` branch and return their raw blob URLs.

    Built entirely from plumbing commands (hash-object/read-tree/write-tree/
    commit-tree) against a throwaway index file, so nothing is checked out —
    safe to call no matter what the current working tree looks like.

    Tree paths are index-prefixed so two screenshots sharing a basename don't
    overwrite each other. Retries on push rejection (another `pr create
    --screenshot` moved the branch tip in the meantime) by re-fetching the new
    tip and rebuilding the commit on top of it.
    """
    for attempt in range(1, max_attempts + 1):
        remote_ref = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/{SCREENSHOTS_BRANCH}"], capture_output=True, encoding="utf-8"
        ).stdout.split()
        parent = remote_ref[0] if remote_ref else None

        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if parent:
                _git(["fetch", "origin", SCREENSHOTS_BRANCH], env=env)
                _git(["read-tree", parent], env=env)

            urls = []
            for i, path in enumerate(paths):
                blob_sha = _git(["hash-object", "-w", path])
                tree_path = f"{branch}/{i:02d}-{Path(path).name}"
                _git(["update-index", "--add", "--cacheinfo", f"100644,{blob_sha},{tree_path}"], env=env)
                urls.append(f"https://github.com/{owner}/{repo}/blob/{SCREENSHOTS_BRANCH}/{tree_path}?raw=true")

            tree_sha = _git(["write-tree"], env=env)

        commit_args = ["commit-tree", tree_sha, "-m", f"screenshots: {branch}"]
        if parent:
            commit_args += ["-p", parent]
        commit_sha = _git(commit_args)

        push = subprocess.run(
            ["git", "push", "origin", f"{commit_sha}:refs/heads/{SCREENSHOTS_BRANCH}"],
            capture_output=True,
            encoding="utf-8",
        )
        if push.returncode == 0:
            return urls
        if attempt == max_attempts:
            sys.exit((push.stderr or push.stdout).strip() or "`git push` failed")

    return []  # unreachable; loop always returns or exits


def add_screenshots(gh: str, owner: str, repo: str, branch: str, screenshot_paths: list[str]) -> None:
    """Push screenshots to the `pr-assets` branch and append them to the current branch's PR body."""
    urls = push_screenshots(owner, repo, branch, screenshot_paths)
    images = list(zip((Path(p).name for p in screenshot_paths), urls))

    pr = _run_gh_json(gh, ["pr", "view", "--json", "number,body"])
    body = build_screenshots_section(pr.get("body"), images)
    r = subprocess.run([gh, "pr", "edit", str(pr["number"]), "--body", body], capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or "`gh pr edit` failed")
    print(f"Attached {len(screenshot_paths)} screenshot(s) to PR #{pr['number']}")


def protection_requires_status_checks(protection: dict) -> bool:
    """True if a branch's `.../protection` response requires status checks to pass."""
    return bool(protection.get("required_status_checks"))


def has_build_policy(gh: str, branch: str) -> bool:
    """Best-effort check for whether `branch` has branch protection requiring status checks.

    Only used to decide whether to print a `bdt pr status` reminder after
    `pr create` — `{owner}`/`{repo}` are resolved by `gh` from the current
    repo, and any failure (no permission to read protection settings, branch
    not protected at all, etc.) fails open (returns False) rather than
    blocking PR creation.
    """
    r = subprocess.run(
        [gh, "api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection"],
        capture_output=True,
        encoding="utf-8",
    )
    if r.returncode != 0:
        return False
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False
    return protection_requires_status_checks(data)
