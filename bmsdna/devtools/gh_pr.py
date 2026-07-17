"""GitHub PR merge/check status and creation via the `gh` CLI.

Deliberately avoids `gh pr checks --json` — that flag was only added in a
later `gh` release than some machines still run (confirmed missing on gh
2.45.0). Everything here is built on `gh pr view --json ...`, whose --json
support has been stable for a long time, plus statusCheckRollup entries
categorized ourselves using GitHub's documented GraphQL enums.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

PR_VIEW_FIELDS = "number,title,baseRefName,mergeable,statusCheckRollup"

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
