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
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .cli_tools import CLI_TIMEOUT_SECS, EXIT_NEEDS_APPROVAL, PollHeartbeat, detect_agent_session, ensure_agent_session_note, is_claude_code
from .pr_markdown import build_attachments_section, build_comment_content, build_screenshots_section

PR_VIEW_FIELDS = "number,title,baseRefName,mergeable,statusCheckRollup,isDraft"

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


# A CheckRun's detailsUrl (e.g. https://github.com/{owner}/{repo}/actions/runs/{run_id}/job/{job_id})
# is the only place `gh pr view`'s statusCheckRollup exposes the backing workflow run id --
# there's no dedicated "runId" field on the check itself.
_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` bounded by `CLI_TIMEOUT_SECS` -- every `gh`/`git` call in this
    module goes through this instead of calling `subprocess.run` directly, so a stalled
    network call or `gh`/`git` blocking on an interactive prompt (e.g. an expired login)
    can't hang the caller forever. Converts a timeout into the same kind of clear,
    exit-with-message failure callers already get from a non-zero return code, rather
    than an uncaught `TimeoutExpired` traceback.
    """
    try:
        return subprocess.run(args, timeout=CLI_TIMEOUT_SECS, **kwargs)
    except subprocess.TimeoutExpired:
        sys.exit(f"`{' '.join(args)}` timed out after {CLI_TIMEOUT_SECS:.0f}s -- stalled network, or needs an interactive login?")


def _run_gh_json(gh: str, args: list[str]) -> dict:
    r = _run([gh, *args], capture_output=True, encoding="utf-8")
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
    # CheckRun.status "WAITING" is GitHub's distinct state for a run paused on a deployment
    # protection rule (e.g. a required reviewer on the target environment) — unlike ordinary
    # "still running" states, nothing here resolves on its own without a human.
    if check.get("status") == "WAITING":
        return "waiting_approval"
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


def draft_notice(pr: dict) -> str | None:
    """None if the PR isn't a draft; else a heads-up that it is.

    Purely informational, not an error — review typically already happened before
    `pr status` is even run, so this doesn't block the rest of the command.
    """
    if not pr.get("isDraft"):
        return None
    pr_ref = f"PR #{pr.get('number')} is a draft - no CI yet."
    if is_claude_code():
        return f"{pr_ref} Run `/code-review` first, then `bdt pr publish`."
    return f"{pr_ref} To publish, use command: `bdt pr publish` (but do an automatic code review first)"


def retry_hint() -> str:
    """The command to tell someone to run after a failed check, to retry just its failed
    job(s) instead of a full rerun of the whole workflow run.
    """
    if is_claude_code():
        return "Run `bdt pr retry` to retry just the failed job(s)."
    return "Hint: to retry just the failed job(s) instead of a full rerun, run: `bdt pr retry`"


def failed_run_ids(checks: list[dict]) -> list[int]:
    """Distinct GitHub Actions run IDs backing a failing check in `checks`, in first-seen
    order. Only `CheckRun` entries (GitHub Actions) carry a `detailsUrl` pointing at a
    run; legacy `StatusContext` entries (e.g. an external CI reporting a commit status)
    have nothing `gh run rerun` can act on and are silently skipped.
    """
    ids: list[int] = []
    seen: set[int] = set()
    for check in checks:
        if check_bucket(check) != "fail":
            continue
        match = _RUN_ID_RE.search(check.get("detailsUrl") or "")
        if not match:
            continue
        run_id = int(match.group(1))
        if run_id not in seen:
            seen.add(run_id)
            ids.append(run_id)
    return ids


def retry(gh: str) -> None:
    """Rerun only the failed job(s) (and whatever depends on them) of the current
    branch's PR's failing workflow run(s), via `gh run rerun --failed` -- not a full
    rerun of the whole run.
    """
    pr = get_pr(gh)
    run_ids = failed_run_ids(pr.get("statusCheckRollup") or [])
    if not run_ids:
        sys.exit("No failed GitHub Actions run found on the PR to retry.")

    # Keep going through every failing run even if one rerun call fails -- a transient
    # error on one run (e.g. "run is already in progress") shouldn't abandon retrying
    # the others, and the failure summary at the end still surfaces it.
    errors: list[str] = []
    for run_id in run_ids:
        r = _run([gh, "run", "rerun", str(run_id), "--failed"], capture_output=True, encoding="utf-8")
        if r.returncode != 0:
            errors.append(f"run {run_id}: {(r.stderr or r.stdout).strip() or 'failed'}")
            continue
        print(f"Reran failed job(s) of run {run_id}")

    if errors:
        sys.exit("Failed to retry: " + "; ".join(errors))

    print("\nRun `bdt pr status --wait` to watch the retry.")


def get_workflow_runs_for_branch(gh: str, branch: str, limit: int = 5) -> list[dict]:
    """Workflow runs triggered directly by a push to `branch` -- e.g. a post-merge/deployment
    workflow that only runs on the target branch once a PR merges into it -- as opposed to a
    run some open PR's `pull_request` trigger produced. `event=push` is what distinguishes the
    two on the same branch name.
    """
    r = _run(
        [
            gh, "run", "list",
            "--branch", branch,
            "--event", "push",
            "--limit", str(limit),
            "--json", "databaseId,name,workflowName,status,conclusion,url,headBranch",
        ],
        capture_output=True,
        encoding="utf-8",
    )
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or "`gh run list` failed")
    return json.loads(r.stdout)


def latest_per_workflow(runs: list[dict]) -> list[dict]:
    """Reduce a workflow run list to the single latest run per workflow."""
    latest: dict = {}
    for r in runs:
        name = r.get("workflowName") or r.get("name")
        if name not in latest or r["databaseId"] > latest[name]["databaseId"]:
            latest[name] = r
    return sorted(latest.values(), key=lambda r: r["databaseId"], reverse=True)


def deploy_run_hint(gh: str, target_branch: str) -> str | None:
    """Best-effort: None unless a workflow run has already been triggered directly by a push to
    `target_branch` (as opposed to this PR's own checks) -- e.g. a post-merge deployment
    workflow. When one exists, a hint suggesting `bdt pr watch-deploy` to watch it.

    Only used to decide whether to print this hint after `bdt pr status` reports success --
    failures here (auth, permissions, an old `gh` without `run list --json`, malformed output,
    ...) fail open (return None) rather than blocking or crashing `pr status`, same as
    `has_build_policy`.
    """
    try:
        runs = get_workflow_runs_for_branch(gh, target_branch, limit=1)
    except (SystemExit, json.JSONDecodeError):
        return None
    if not runs:
        return None
    run = runs[0]
    name = run.get("workflowName") or run.get("name") or "?"
    return (
        f"\nA workflow run has already been triggered on '{target_branch}' ({name} #{run.get('databaseId')}) -- "
        f"probably a post-merge deployment. Run `bdt pr watch-deploy --target-branch {target_branch}` to watch it."
    )


def print_check(check: dict) -> None:
    bucket = check_bucket(check)
    icon = {"pass": "✓", "fail": "✗", "cancel": "⊘", "waiting_approval": "⏸"}.get(bucket, "…")
    print(f"  [{icon} {bucket.upper()}] {check_label(check)}")


def exit_needs_approval(msg: str, item_lines: list[str], rerun_cmd: str) -> None:
    """Print the "Waiting for approval" report for already-formatted `item_lines` and exit
    EXIT_NEEDS_APPROVAL -- shared by `run()` and `run_watch_deploy()`, which differ only in
    what a "blocked" item is (a check vs. a workflow run) and how to label one.
    """
    print(msg)
    print("\nWaiting for approval:")
    for line in item_lines:
        print(f"  {line}")
    print(f"\nApprove at the link(s) above, then re-run `{rerun_cmd}`.")
    sys.exit(EXIT_NEEDS_APPROVAL)


def run(gh: str, wait: bool) -> None:
    heartbeat = PollHeartbeat()
    draft_notice_shown = False
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

        if not draft_notice_shown:
            draft_msg = draft_notice(pr)
            if draft_msg:
                print(draft_msg)
            draft_notice_shown = True

        pr_number = pr.get("number")
        title = pr.get("title", "?")
        base = pr.get("baseRefName", "?")
        msg = f"\rPR #{pr_number}: {title} (base={base})"

        checks = pr.get("statusCheckRollup") or []
        if not checks:
            print(msg + " | no checks found.")
            # No PR-triggered checks at all is itself a settled state (e.g. this repo's
            # only workflow triggers on a push to the target branch, not on `pull_request`)
            # -- exactly when a deploy-build hint is most useful, so still check for one.
            hint = deploy_run_hint(gh, base)
            if hint:
                print(hint)
            return

        buckets = [check_bucket(c) for c in checks]
        msg += " | " + ", ".join(f"{check_label(c)}: {check_bucket(c)}" for c in checks)

        waiting_approval = [c for c, b in zip(checks, buckets) if b == "waiting_approval"]
        # A failed check anywhere in the PR is reported as such even when another check is
        # separately waiting on approval — a human shouldn't be sent to go approve a
        # deployment gate while staying unaware that CI has already failed elsewhere.
        if wait and waiting_approval and "fail" not in buckets:
            item_lines = []
            for c in waiting_approval:
                details_url = c.get("detailsUrl")
                suffix = f" — {details_url}" if details_url else ""
                item_lines.append(f"{check_label(c)} needs a reviewer to approve the deployment{suffix}")
            exit_needs_approval(msg, item_lines, "bdt pr status --wait")

        # A check waiting on approval never resolves on its own -- if some other still-pending
        # check only looks pending because it's downstream of that same approval gate, --wait
        # must not keep polling forever waiting for it to become unstuck.
        if "pending" in buckets and wait and not waiting_approval:
            heartbeat.show(msg)
            time.sleep(30)
            continue

        print(msg)
        print("\nDetails:")
        for c in checks:
            print_check(c)

        if "fail" in buckets:
            print(f"\n{retry_hint()}")
            sys.exit(1)
        # Only worth suggesting `pr watch-deploy` once this PR's own checks are actually
        # settled (not still pending, and not sitting on an unresolved approval prompt,
        # because the caller ran without --wait) -- otherwise it'd claim a merge/deploy is
        # underway before the PR has even finished its own CI.
        if "pending" not in buckets and "waiting_approval" not in buckets:
            hint = deploy_run_hint(gh, base)
            if hint:
                print(hint)
        return


def print_failed_step_logs(gh: str, run_id: int) -> None:
    r = _run([gh, "run", "view", str(run_id), "--log-failed"], capture_output=True, encoding="utf-8")
    output = (r.stdout or "").strip()
    if r.returncode != 0 or not output:
        print("  (no failed steps with logs)")
        return
    print(f"\n--- Failed steps (run {run_id}) ---")
    print(output)


def print_run(gh: str, run: dict) -> None:
    run_id: int = run["databaseId"]
    status = run.get("status", "unknown")
    conclusion = run.get("conclusion") or "—"
    name = run.get("workflowName") or run.get("name", "?")
    url = run.get("url", "?")

    icon = {"success": "✓", "failure": "✗", "cancelled": "⊘"}.get(conclusion, "…")

    print(f"\n{'-' * 60}")
    print(f"Run #{run_id}  [{icon} {conclusion.upper()}]")
    print(f"  Workflow : {name}")
    print(f"  Status   : {status}")
    print(f"  URL      : {url}")

    if status == "completed" and conclusion == "failure":
        print_failed_step_logs(gh, run_id)


def run_watch_deploy(gh: str, target_branch: str, wait: bool) -> None:
    """Watch the most recent workflow run(s) triggered directly by a push to `target_branch`
    -- e.g. a post-merge workflow that only runs on the target branch once a PR merges into
    it, and usually does the actual deployment -- until they complete.

    Mirrors `run()`'s polling/reporting shape, but looks at runs tied to the branch's push
    event via `get_workflow_runs_for_branch` rather than a PR's `statusCheckRollup`.
    """
    heartbeat = PollHeartbeat()
    while True:
        runs = get_workflow_runs_for_branch(gh, target_branch)
        if not runs:
            print(f"No workflow runs found on '{target_branch}' triggered by a push.")
            return

        latest_runs = latest_per_workflow(runs)
        msg = f"\rBranch '{target_branch}' | " + ", ".join(
            f"{(r.get('workflowName') or r.get('name'))} #{r['databaseId']} {r.get('status')} ({r.get('conclusion') or '—'})"
            for r in latest_runs
        )

        already_failed = any(r.get("conclusion") == "failure" for r in latest_runs)
        waiting_approval = [r for r in latest_runs if r.get("status") == "waiting"]
        # WorkflowRun.status "waiting" is a distinct state for a run paused on a deployment
        # protection rule (e.g. a required reviewer on the target environment) -- unlike
        # ordinary in-progress runs, nothing here resolves on its own without a human. A run
        # that's already failed elsewhere is reported as such (exit 1) instead, same priority
        # as `run()` -- a human shouldn't be sent to go approve a deployment while staying
        # unaware CI already failed.
        if wait and waiting_approval and not already_failed:
            item_lines = [
                f"{r.get('workflowName') or r.get('name') or '?'} #{r['databaseId']} needs a reviewer to approve the deployment — {r.get('url', '?')}"
                for r in waiting_approval
            ]
            exit_needs_approval(msg, item_lines, "bdt pr watch-deploy --wait")

        # A run stuck on approval must not be treated as "still in progress" once something
        # else has already failed -- otherwise --wait would poll forever for a run that can
        # never resolve on its own instead of reporting the failure.
        all_done = already_failed or all(r.get("status") == "completed" for r in latest_runs)
        if all_done or not wait:
            print(msg)
            print("\nDetails:")
            for r in latest_runs:
                print_run(gh, r)

            if not all_done and not wait:
                print("\nTip: Use --wait to poll until all workflows are completed.")

            if any(r.get("conclusion") == "failure" for r in latest_runs):
                sys.exit(1)
            return

        heartbeat.show(msg)
        time.sleep(30)


def create(gh: str, target: str, extra_args: list[str], draft: bool = False, labels: list[str] | None = None) -> tuple[int, str | None]:
    """Create a GitHub PR from the current branch into `target`.

    --fill autofills title/body from commit info so this never blocks on an
    interactive prompt; pass --title/--body in extra_args to override (gh
    lets explicit values take precedence over --fill).

    `-l/--label` labels must already exist on the repo (`gh label create`) —
    unlike Azure DevOps PR labels, GitHub rejects a label name it doesn't
    already know about.

    Returns (returncode, web_url) — on success `gh pr create` prints the
    PR's web URL as its only stdout line, which is what a human needs to
    open it; on failure the url is `None` and the CLI's stderr is
    surfaced to ours.
    """
    cmd = [gh, "pr", "create", "--base", target, "--fill", *(["--draft"] if draft else [])]
    for label in labels or []:
        cmd += ["--label", label]
    cmd += extra_args
    r = _run(cmd, capture_output=True, encoding="utf-8")
    if r.stderr:
        print(r.stderr.strip(), file=sys.stderr)
    if r.returncode != 0:
        return r.returncode, None
    ensure_session_note(gh)
    return r.returncode, r.stdout.strip() or None


def ensure_session_note(gh: str) -> None:
    """Make sure the current branch's PR body records the running agent's session, if any
    (see `ensure_agent_session_note`) -- e.g. right after `create()`, whose body/title
    come from `--fill`/`extra_args` rather than a value this module builds itself, so
    there's nothing to pass the note through beforehand.

    Best-effort: swallows failures (a stale view, a rejected edit, ...) rather than
    turning a successful `pr create`/whatever else called this into a failure over a
    step that's purely nice-to-have. A no-op (no extra `gh` calls at all) when no
    agent is detected, which is the common case running outside one.
    """
    if detect_agent_session() is None:
        return
    try:
        pr = _run_gh_json(gh, ["pr", "view", "--json", "number,title,body"])
        body = pr.get("body") or ""
        noted = ensure_agent_session_note(body, also_check=pr.get("title"))
        if noted != body:
            r = _run([gh, "pr", "edit", str(pr["number"]), "--body", noted or ""], capture_output=True, encoding="utf-8")
            r.check_returncode()
    except (subprocess.SubprocessError, SystemExit, json.JSONDecodeError, OSError):
        pass


def publish(gh: str) -> None:
    """Mark the current branch's draft PR as ready for review."""
    r = _run([gh, "pr", "ready"], capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or "`gh pr ready` failed")
    print("Marked PR as ready for review")
    print("\nRun `bdt pr status --wait` to watch the PR's CI.")


def _git(args: list[str], env: dict[str, str] | None = None) -> str:
    r = _run(["git", *args], capture_output=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or f"`git {' '.join(args)}` failed")
    return r.stdout.strip()


def push_assets(owner: str, repo: str, branch: str, paths: list[str], max_attempts: int = 5) -> list[str]:
    """Push `paths` (screenshots or arbitrary files) to a `<branch>/` folder on the `pr-assets`
    branch and return their raw blob URLs.

    Built entirely from plumbing commands (hash-object/read-tree/write-tree/
    commit-tree) against a throwaway index file, so nothing is checked out —
    safe to call no matter what the current working tree looks like.

    Tree paths are index-prefixed so two paths sharing a basename don't
    overwrite each other. Retries on push rejection (another `pr create
    --screenshot`/`--file` moved the branch tip in the meantime) by re-fetching the new
    tip and rebuilding the commit on top of it.
    """
    for attempt in range(1, max_attempts + 1):
        remote_ref = _run(
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

        commit_args = ["commit-tree", tree_sha, "-m", f"assets: {branch}"]
        if parent:
            commit_args += ["-p", parent]
        commit_sha = _git(commit_args)

        push = _run(
            ["git", "push", "origin", f"{commit_sha}:refs/heads/{SCREENSHOTS_BRANCH}"],
            capture_output=True,
            encoding="utf-8",
        )
        if push.returncode == 0:
            return urls
        if attempt == max_attempts:
            sys.exit((push.stderr or push.stdout).strip() or "`git push` failed")

    return []  # unreachable; loop always returns or exits


def _screenshot_images(owner: str, repo: str, branch: str, screenshot_paths: list[str]) -> list[tuple[str, str]]:
    urls = push_assets(owner, repo, branch, screenshot_paths)
    return list(zip((Path(p).name for p in screenshot_paths), urls))


def _file_links(owner: str, repo: str, branch: str, file_paths: list[str]) -> list[tuple[str, str]]:
    urls = push_assets(owner, repo, branch, file_paths)
    return list(zip((Path(p).name for p in file_paths), urls))


def add_attachments(
    gh: str,
    owner: str,
    repo: str,
    branch: str,
    screenshot_paths: list[str] | None = None,
    file_paths: list[str] | None = None,
) -> None:
    """Push screenshots/files to the `pr-assets` branch and append them to the current branch's PR
    body in a single edit -- whether one or both kinds are given.
    """
    screenshot_paths = screenshot_paths or []
    file_paths = file_paths or []
    pr = _run_gh_json(gh, ["pr", "view", "--json", "number,body"])
    body: str = pr.get("body") or ""
    if screenshot_paths:
        body = build_screenshots_section(body, _screenshot_images(owner, repo, branch, screenshot_paths))
    if file_paths:
        body = build_attachments_section(body, _file_links(owner, repo, branch, file_paths))
    r = _run([gh, "pr", "edit", str(pr["number"]), "--body", body], capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or "`gh pr edit` failed")
    print(f"Attached {len(screenshot_paths)} screenshot(s), {len(file_paths)} file(s) to PR #{pr['number']}")


def add_screenshots(gh: str, owner: str, repo: str, branch: str, screenshot_paths: list[str]) -> None:
    """Push screenshots to the `pr-assets` branch and append them to the current branch's PR body."""
    add_attachments(gh, owner, repo, branch, screenshot_paths=screenshot_paths)


def add_files(gh: str, owner: str, repo: str, branch: str, file_paths: list[str]) -> None:
    """Push files to the `pr-assets` branch and append them as linked attachments to the current branch's PR body."""
    add_attachments(gh, owner, repo, branch, file_paths=file_paths)


def update(
    gh: str,
    owner: str,
    repo: str,
    branch: str,
    title: str | None = None,
    description: str | None = None,
    screenshot_paths: list[str] | None = None,
    file_paths: list[str] | None = None,
) -> None:
    """Update a PR's title and/or body, optionally appending screenshots/files to the body."""
    pr = _run_gh_json(gh, ["pr", "view", "--json", "number,title,body"])
    args = [gh, "pr", "edit", str(pr["number"])]
    if title:
        args += ["--title", title]
    if description is not None or screenshot_paths or file_paths:
        new_body: str = description if description is not None else (pr.get("body") or "")
        if screenshot_paths:
            new_body = build_screenshots_section(new_body, _screenshot_images(owner, repo, branch, screenshot_paths))
        if file_paths:
            new_body = build_attachments_section(new_body, _file_links(owner, repo, branch, file_paths))
        new_body = ensure_agent_session_note(new_body, also_check=title or pr.get("title")) or ""
        args += ["--body", new_body]
    if len(args) == 3:
        return
    r = _run(args, capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or "`gh pr edit` failed")
    print(f"Updated PR #{pr['number']}")


def comment_with_screenshots(
    gh: str,
    owner: str,
    repo: str,
    branch: str,
    message: str | None,
    screenshot_paths: list[str],
    file_paths: list[str] | None = None,
) -> None:
    """Post a comment, with a message and/or screenshots/files, on the current branch's PR."""
    file_paths = file_paths or []
    images = _screenshot_images(owner, repo, branch, screenshot_paths) if screenshot_paths else []
    files = _file_links(owner, repo, branch, file_paths) if file_paths else []
    content = ensure_agent_session_note(build_comment_content(message, images, files)) or ""
    r = _run([gh, "pr", "comment", "--body", content], capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip() or "`gh pr comment` failed")
    print(f"Added comment ({len(screenshot_paths)} screenshot(s), {len(file_paths)} file(s)) to the current PR")


def protection_requires_status_checks(protection: dict) -> bool:
    """True if a branch's `.../protection` response requires status checks to pass."""
    return bool(protection.get("required_status_checks"))


def has_build_policy(gh: str, branch: str) -> bool:
    """Best-effort check for whether `branch` has branch protection requiring status checks.

    Only used to decide whether to print a `bdt pr status` reminder after
    `pr create` — `{owner}`/`{repo}` are resolved by `gh` from the current
    repo, and any failure (no permission to read protection settings, branch
    not protected at all, a timed-out call, etc.) fails open (returns False)
    rather than blocking PR creation.
    """
    try:
        r = _run(
            [gh, "api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection"],
            capture_output=True,
            encoding="utf-8",
        )
    except SystemExit:
        return False
    if r.returncode != 0:
        return False
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False
    return protection_requires_status_checks(data)
