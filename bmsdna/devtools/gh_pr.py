"""GitHub PR merge/check status and creation over the GitHub REST + GraphQL API.

Built on `GitHubClient` (raw HTTP, see `github_api`). A PR is read through one GraphQL query
(`get_pr`) because REST has no check-rollup / closing-issue / mergeable-state view of it; the
result keeps the shape `gh pr view --json` used to return (`statusCheckRollup` entries with
`__typename`/`status`/`conclusion`/`name`/`workflowName`/`detailsUrl`/`state`, ...), so the
categorizing helpers below (`check_bucket`, ...) work on GitHub's documented GraphQL enums.
Everything else (create/edit/comment/rerun/run list/ready) is plain REST, except toggling
draft state, which only exists as a GraphQL mutation.

The PR is looked up in the repo of the `origin` remote (`gh.repo_path`), with the head `origin-owner:branch`:
a fork workflow where `origin` is not the repo the PR is opened in (a PR from a fork's branch into its
upstream) is not supported.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import pr_issue_link
from .cli_tools import (
    CLI_TIMEOUT_SECS,
    EXIT_NEEDS_APPROVAL,
    PollHeartbeat,
    WaitDeadline,
    detect_agent_session,
    ensure_agent_session_note,
    exit_wait_timeout,
    is_claude_code,
    status_rerun_cmd,
)
from .github_api import GitHubClient
from .gitrepo import current_branch
from .pr_choice import PrCandidate, choose_pr
from .pr_markdown import build_attachments_section, build_comment_content, build_screenshots_section

# `get_pr(fields=...)` selects keys of the dict one GraphQL query returns (see `_PR_QUERY`); `id` is the node id
# (needed for the draft mutations), `body` the PR description.
PR_VIEW_FIELDS = "number,title,baseRefName,headRefName,mergeable,statusCheckRollup,isDraft"
# What `bdt pr info` needs on top of the check rollup: where to link to, and the issues GitHub says it closes.
PR_INFO_FIELDS = "number,title,url,state,isDraft,closingIssuesReferences,statusCheckRollup"

# GitHub has no API for uploading images to a PR description (only the web
# UI's drag-and-drop, which needs a browser session). The standard
# workaround: keep screenshots on their own orphan branch, one folder per
# source branch, and link to them with a raw blob URL.
SCREENSHOTS_BRANCH = "pr-assets"

# `CLI_TIMEOUT_SECS` is generous for a plain `git` metadata call, but too tight for
# fetching/pushing actual screenshot/attachment blob content on `SCREENSHOTS_BRANCH` --
# give those more room instead of aborting an otherwise-fine transfer just because it's
# slower than a metadata call.
CLI_UPLOAD_TIMEOUT_SECS = CLI_TIMEOUT_SECS * 4

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
# is the only place a statusCheckRollup entry exposes the backing workflow run id --
# there's no dedicated "runId" field on the check itself.
_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")


def _run(args: list[str], *, timeout: float = CLI_TIMEOUT_SECS, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` bounded by `timeout` (defaults to `CLI_TIMEOUT_SECS`) -- every `git` call
    in this module goes through this instead of calling `subprocess.run` directly, so a stalled
    network call or `git` blocking on an interactive prompt can't hang the caller forever.
    Converts a timeout into the same kind of clear, exit-with-message failure callers already
    get from a non-zero return code, rather than an uncaught `TimeoutExpired` traceback.

    A caller pushing/fetching actual payload (screenshot blobs, not just metadata) should
    pass a longer `timeout` -- see `CLI_UPLOAD_TIMEOUT_SECS`.
    """
    try:
        return subprocess.run(args, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        sys.exit(f"`{' '.join(args)}` timed out after {timeout:.0f}s -- stalled network, or needs an interactive login?")


def _branch_prs(gh: GitHubClient, branch: str, state: str = "all", base: str | None = None) -> list[dict]:
    """REST pull objects whose head is `branch` of this repo, open ones first, then newest first."""
    params: dict[str, Any] = {"head": f"{gh.owner}:{branch}", "state": state, "sort": "created", "direction": "desc"}
    if base:
        params["base"] = base
    prs = gh.get(f"{gh.repo_path}/pulls", params=params)
    return sorted(prs, key=lambda p: p.get("state") != "open")  # stable: keeps newest-first within each group


def _pr_number(gh: GitHubClient, pr_id: int | None) -> int:
    """`pr_id` if given, else the current branch's PR -- the open one if there is one, else the
    latest closed/merged one (what `gh pr view` without a number used to find)."""
    if pr_id is not None:
        return pr_id
    branch = current_branch()
    prs = _branch_prs(gh, branch)
    if not prs:
        sys.exit(f"no pull requests found for branch \"{branch}\"")
    return prs[0]["number"]


def _pr_rest(gh: GitHubClient, pr_id: int | None) -> dict:
    """The PR as `{number, title, body, headRefName, id, draft}` (REST: cheaper than `get_pr`, which also
    needs the check rollup), for the current branch's PR or `pr_id`."""
    number = _pr_number(gh, pr_id)
    pr = gh.get(f"{gh.repo_path}/pulls/{number}")
    return {
        "number": pr["number"],
        "title": pr.get("title") or "",
        "body": pr.get("body") or "",
        "headRefName": (pr.get("head") or {}).get("ref"),
        "id": pr.get("node_id"),
        "draft": bool(pr.get("draft")),
    }


def head_branch(gh: GitHubClient, pr_id: int | None) -> str | None:
    """The head branch of PR `pr_id` (or the current branch's PR), in a single REST call."""
    return _pr_rest(gh, pr_id)["headRefName"]


def resolve_pr_id(gh: GitHubClient, pr_id: int | None, target: str | None = None) -> int | None:
    """The number of the PR to act on: `pr_id` if given, else the open PR from the current branch
    (`target` narrows it to one into that base branch; several are ambiguous: see `choose_pr`).

    None if the branch has no open PR (and no `target` was asked for): the caller then lets the
    other helpers resolve it as before, which also finds a closed/merged one. Call it once, before any polling.
    """
    if pr_id is not None:
        return pr_id
    branch = current_branch()
    prs = _branch_prs(gh, branch, state="open", base=target)
    if not prs:
        if target:
            sys.exit(f"No open PR found from '{branch}' → '{target}'")
        return None
    candidates = [PrCandidate(p["number"], p["title"], p["base"]["ref"], p["html_url"]) for p in prs]
    return prs[choose_pr(branch, candidates)]["number"]


_CONTEXT_FIELDS = """
fragment ContextFields on StatusCheckRollupContext {
  __typename
  ... on CheckRun { name status conclusion detailsUrl checkSuite { workflowRun { workflow { name } } } }
  ... on StatusContext { context state targetUrl }
}
"""

_PR_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id number title body url state isDraft mergeable baseRefName headRefName
      closingIssuesReferences(first: 50) { nodes { number url repository { name owner { login } } } }
      statusCheckRollup {
        contexts(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes { ...ContextFields }
        }
      }
    }
  }
}
""" + _CONTEXT_FIELDS

# The rest of a rollup with more than 100 contexts (matrix builds + legacy statuses), 100 at a time.
_CONTEXTS_QUERY = """
query ContextsPage($owner: String!, $repo: String!, $number: Int!, $after: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      statusCheckRollup {
        contexts(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes { ...ContextFields }
        }
      }
    }
  }
}
""" + _CONTEXT_FIELDS


def _check_entry(node: dict) -> dict:
    """A GraphQL rollup context, flattened to the `gh pr view --json statusCheckRollup` shape."""
    if node.get("__typename") != "CheckRun":
        # StatusContext has `context`/`targetUrl` where a CheckRun has `name`/`detailsUrl`
        return {**node, "name": node.get("context"), "detailsUrl": node.get("targetUrl")}
    workflow = (((node.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {}).get("name")
    return {**{k: v for k, v in node.items() if k != "checkSuite"}, "workflowName": workflow or ""}


def get_pr(gh: GitHubClient, pr_id: int | None = None, fields: str = PR_VIEW_FIELDS) -> dict:
    """The PR for the current branch (open one preferred; callers pass the `resolve_pr_id`
    result as `pr_id`, which settles which PR it is when the branch has several).

    If `pr_id` is given, resolves that PR directly instead -- lets a caller act on a
    specific PR without needing its branch checked out locally at all.

    `fields` is a comma-separated key list (see `PR_VIEW_FIELDS`/`PR_INFO_FIELDS`); only those
    keys are returned, in the shape `gh pr view --json` had (`statusCheckRollup` and
    `closingIssuesReferences` are flat lists, `[]` when empty).
    """
    number = _pr_number(gh, pr_id)
    data = gh.graphql(_PR_QUERY, owner=gh.owner, repo=gh.repo, number=number)
    pr = (data.get("repository") or {}).get("pullRequest")
    if not pr:
        sys.exit(f"Pull request #{number} not found in {gh.owner}/{gh.repo}")
    contexts = (pr.get("statusCheckRollup") or {}).get("contexts") or {}
    nodes = list(contexts.get("nodes") or [])
    while (contexts.get("pageInfo") or {}).get("hasNextPage"):
        page = gh.graphql(_CONTEXTS_QUERY, owner=gh.owner, repo=gh.repo, number=number, after=contexts["pageInfo"]["endCursor"])
        contexts = (((page.get("repository") or {}).get("pullRequest") or {}).get("statusCheckRollup") or {}).get("contexts") or {}
        nodes += contexts.get("nodes") or []
    pr["statusCheckRollup"] = [_check_entry(n) for n in nodes if n]
    pr["closingIssuesReferences"] = [n for n in (pr.get("closingIssuesReferences") or {}).get("nodes") or [] if n]
    wanted = [f.strip() for f in fields.split(",") if f.strip()]
    return {k: pr.get(k) for k in wanted}


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
    have nothing a rerun can act on and are silently skipped.
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


def retry(gh: GitHubClient, pr_id: int | None = None) -> None:
    """Rerun only the failed job(s) (and whatever depends on them) of the current
    branch's PR's failing workflow run(s) -- or `pr_id`'s, if given -- via
    `POST .../rerun-failed-jobs` -- not a full rerun of the whole run.
    """
    pr = get_pr(gh, pr_id)
    run_ids = failed_run_ids(pr.get("statusCheckRollup") or [])
    if not run_ids:
        sys.exit("No failed GitHub Actions run found on the PR to retry.")

    # Keep going through every failing run even if one rerun call fails -- a transient
    # error on one run (e.g. "run is already in progress") shouldn't abandon retrying
    # the others, and the failure summary at the end still surfaces it.
    errors: list[str] = []
    for run_id in run_ids:
        try:
            gh.post(f"{gh.repo_path}/actions/runs/{run_id}/rerun-failed-jobs")
        except SystemExit as e:
            errors.append(f"run {run_id}: {e.code or 'failed'}")
            continue
        print(f"Reran failed job(s) of run {run_id}")

    if errors:
        sys.exit("Failed to retry: " + "; ".join(errors))

    print("\nRun `bdt pr status --wait` to watch the retry.")


def get_workflow_runs_for_branch(gh: GitHubClient, branch: str, limit: int = 5) -> list[dict]:
    """Workflow runs triggered directly by a push to `branch` -- e.g. a post-merge/deployment
    workflow that only runs on the target branch once a PR merges into it -- as opposed to a
    run some open PR's `pull_request` trigger produced. `event=push` is what distinguishes the
    two on the same branch name.

    Each run is `{databaseId, name, workflowName, status, conclusion, url, headBranch}`.
    """
    data = gh.get(f"{gh.repo_path}/actions/runs", params={"branch": branch, "event": "push", "per_page": limit})
    return [
        {
            "databaseId": r["id"],
            "name": r.get("name"),
            "workflowName": r.get("name"),  # REST `name` is the workflow's name (`display_title` is the commit message)
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "url": r.get("html_url"),
            "headBranch": r.get("head_branch"),
        }
        for r in (data.get("workflow_runs") or [])[:limit]
    ]


def latest_per_workflow(runs: list[dict]) -> list[dict]:
    """Reduce a workflow run list to the single latest run per workflow."""
    latest: dict = {}
    for r in runs:
        name = r.get("workflowName") or r.get("name")
        if name not in latest or r["databaseId"] > latest[name]["databaseId"]:
            latest[name] = r
    return sorted(latest.values(), key=lambda r: r["databaseId"], reverse=True)


def deploy_run_hint(gh: GitHubClient, target_branch: str) -> str | None:
    """Best-effort: None unless a workflow run has already been triggered directly by a push to
    `target_branch` (as opposed to this PR's own checks) -- e.g. a post-merge deployment
    workflow. When one exists, a hint suggesting `bdt pr watch-deploy` to watch it.

    Only used to decide whether to print this hint after `bdt pr status` reports success --
    failures here (auth, permissions, malformed output, ...) fail open (return None) rather than blocking or crashing `pr status`, same as
    `has_build_policy`.
    """
    try:
        runs = get_workflow_runs_for_branch(gh, target_branch, limit=1)
    except (SystemExit, KeyError, ValueError, AttributeError):
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


def run(gh: GitHubClient, wait: bool, pr_id: int | None = None, timeout: float = 0) -> None:
    """`timeout` (seconds, only with `wait`): stop polling after that long and exit `EXIT_WAIT_TIMEOUT`
    with the current state; 0 = wait until every check is done."""
    heartbeat = PollHeartbeat()
    deadline = WaitDeadline(timeout)
    draft_notice_shown = False
    while True:
        pr = get_pr(gh, pr_id)

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
            if deadline.expired():
                exit_wait_timeout(msg, lambda: [print_check(c) for c in checks], deadline, status_rerun_cmd(pr_number, timeout))
            deadline.sleep(30)
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


_FAILED_LOG_TAIL_LINES = 200


def print_failed_step_logs(gh: GitHubClient, run_id: int) -> None:
    """Best-effort: the failed steps of `run_id`'s failed jobs, with the tail of each job's log.

    The jobs API lists which steps failed; a job's log (`actions/jobs/{id}/logs`, a redirect to
    plain text) is one blob for the whole job, so only its last `_FAILED_LOG_TAIL_LINES` lines
    -- where the failing step's output ends up -- are shown. Any failure fetching just means
    "nothing to show".
    """
    chunks: list[str] = []
    try:
        data = gh.get(f"{gh.repo_path}/actions/runs/{run_id}/jobs", params={"filter": "latest", "per_page": 100})
        for job in data.get("jobs") or []:
            if job.get("conclusion") != "failure":
                continue
            steps = [s.get("name", "?") for s in job.get("steps") or [] if s.get("conclusion") == "failure"]
            header = f"{job.get('name', '?')}" + (f" -- failed step(s): {', '.join(steps)}" if steps else "")
            r = gh.request("GET", f"{gh.repo_path}/actions/jobs/{job['id']}/logs", ok=(404, 410))
            log = r.text.strip() if r.status_code < 400 else ""
            lines = log.splitlines()[-_FAILED_LOG_TAIL_LINES:]
            chunks.append(header + ("\n" + "\n".join(lines) if lines else "\n(no log available)"))
    except SystemExit:
        pass
    if not chunks:
        print("  (no failed steps with logs)")
        return
    print(f"\n--- Failed steps (run {run_id}) ---")
    print("\n\n".join(chunks))


def print_run(gh: GitHubClient, run: dict) -> None:
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


def run_watch_deploy(gh: GitHubClient, target_branch: str, wait: bool) -> None:
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


def _parse_create_args(extra_args: list[str]) -> dict[str, str]:
    """`--title X` / `--title=X` / `--body X` / `--body=X` (also `-t`/`-b`, and `--description`/`-d` as
    an alias of `--body`, as on Azure DevOps) out of a pass-through
    argument list; anything else can't be honored now that there's no `gh` to hand it to."""
    out: dict[str, str] = {}
    keys = {"--title": "title", "-t": "title", "--body": "body", "-b": "body", "--description": "body", "-d": "body"}
    args = list(extra_args)
    while args:
        arg = args.pop(0)
        name, eq, value = arg.partition("=")
        if name in keys and eq:
            out[keys[name]] = value
        elif arg in keys and args:
            out[keys[arg]] = args.pop(0)
        else:
            sys.exit(f"unsupported argument for `pr create`: {arg!r} (only --title and --body/--description are supported)")
    return out


def _branch_commits(target: str) -> list[tuple[str, str]]:
    """(subject, body) of each commit on HEAD not yet in `target`, oldest first; [] if unknown."""
    for base in (f"origin/{target}", target):
        r = _run(["git", "log", "--reverse", "--format=%s%x1f%b%x1e", f"{base}..HEAD"], capture_output=True, encoding="utf-8")
        if r.returncode == 0:
            commits = []
            for rec in r.stdout.split("\x1e"):
                if rec.strip():
                    subject, _, body = rec.strip().partition("\x1f")
                    commits.append((subject.strip(), body.strip()))
            return commits
    return []


def _fill(branch: str, target: str) -> tuple[str, str]:
    """Default (title, body), like `gh pr create --fill`: a single commit's message, else the
    humanized branch name with the commit subjects as a list."""
    commits = _branch_commits(target)
    if len(commits) == 1:
        return commits[0]
    title = branch.replace("-", " ").replace("_", " ").strip()
    title = title[:1].upper() + title[1:] if title else branch
    return title, "\n".join(f"- {subject}" for subject, _ in commits)


def create(
    gh: GitHubClient,
    target: str,
    extra_args: list[str],
    draft: bool = False,
    labels: list[str] | None = None,
    title: str | None = None,
    description: str | None = None,
) -> tuple[int, str | None]:
    """Create a GitHub PR from the current branch into `target`.

    Title/body default to the branch's commits (a single commit's message; else the branch
    name and a list of the subjects) so this never blocks on a prompt. `title`/`description`
    override that; `--title`/`--body` in `extra_args` override those again (no other
    pass-through argument is supported -- it exits naming it). The branch must already be pushed.

    `labels` are applied after creating the PR (GitHub creates a label it doesn't know yet).

    Returns (returncode, web_url) -- on failure the url is `None`, the error is printed to
    stderr and the code is 1.
    """
    extra = _parse_create_args(extra_args)
    branch = current_branch()
    fill_title, fill_body = ("", "")
    if title is None and "title" not in extra or description is None and "body" not in extra:
        fill_title, fill_body = _fill(branch, target)
    payload = {
        "title": extra.get("title", title if title is not None else fill_title),
        "body": extra.get("body", description if description is not None else fill_body),
        "head": branch,
        "base": target,
        "draft": draft,
    }
    try:
        pr = gh.post(f"{gh.repo_path}/pulls", payload)
    except SystemExit as e:
        print(str(e.code).strip(), file=sys.stderr)
        return 1, None
    if labels:
        try:
            gh.post(f"{gh.repo_path}/issues/{pr['number']}/labels", {"labels": labels})
        except SystemExit as e:
            print(f"PR created, but adding label(s) failed: {e.code}", file=sys.stderr)
    ensure_session_note(gh, pr["number"])
    return 0, pr.get("html_url")


def ensure_session_note(gh: GitHubClient, pr_number: int | None = None) -> None:
    """Make sure the PR's body (`pr_number`'s, else the current branch's) records the running
    agent's session, if any (see `ensure_agent_session_note`) -- e.g. right after `create()`,
    whose body comes from commits/arguments rather than a value this module builds itself.

    Best-effort: swallows failures (a stale view, a rejected edit, ...) rather than
    turning a successful `pr create`/whatever else called this into a failure over a
    step that's purely nice-to-have. A no-op (no API calls at all) when no
    agent is detected, which is the common case running outside one.
    """
    if detect_agent_session() is None:
        return
    try:
        pr = _pr_rest(gh, pr_number)
        body = pr["body"]
        noted = ensure_agent_session_note(body, also_check=pr["title"])
        if noted != body:
            gh.patch(f"{gh.repo_path}/pulls/{pr['number']}", {"body": noted or ""})
    except (SystemExit, KeyError, ValueError, OSError):
        pass


def get_pr_body(gh: GitHubClient) -> tuple[int, str]:
    """(PR number, PR body) for the current branch's PR -- used by issue-linking, which needs
    the body actually stored (e.g. autofilled from the commit message), not
    whatever a caller happened to pass to `pr create`.
    """
    pr = _pr_rest(gh, None)
    return pr["number"], pr["body"]


def link_issue_to_pr(gh: GitHubClient, pr_number: int, body: str, issue_number: int) -> str:
    """Make sure `body` (the PR's current body) references `issue_number` via a GitHub closing
    keyword, so GitHub shows this as a genuinely linked PR (appearing in the issue's
    "Development" sidebar and auto-closing it on merge) -- a plain `#N` mention elsewhere (only
    in a comment, or just prose referencing it, say) doesn't get that treatment, so it isn't
    mistaken for one already being present (see `pr_issue_link.github_body_already_closes`). A
    no-op if `body` already has a real closing-keyword reference to the issue (e.g. because this
    number came from scanning the body for one in the first place).

    Returns the (possibly unchanged) new body -- callers linking more than one issue in a row
    pass this back in as `body` for the next call, so each edit builds on the last instead of
    re-fetching or clobbering a previous append.
    """
    if pr_issue_link.github_body_already_closes(body, issue_number):
        return body
    new_body = f"{body.rstrip()}\n\nFixes #{issue_number}\n".lstrip("\n")
    gh.patch(f"{gh.repo_path}/pulls/{pr_number}", {"body": new_body})
    return new_body


_READY_MUTATION = "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { id } } }"
_DRAFT_MUTATION = "mutation($id: ID!) { convertPullRequestToDraft(input: {pullRequestId: $id}) { pullRequest { id } } }"


def publish(gh: GitHubClient, pr_id: int | None = None) -> None:
    """Mark the current branch's draft PR -- or `pr_id`'s, if given -- as ready for review."""
    pr = _pr_rest(gh, pr_id)
    if not pr["draft"]:
        print(f"PR #{pr['number']} is already ready for review")
        return
    gh.graphql(_READY_MUTATION, id=pr["id"])
    print("Marked PR as ready for review")
    print("\nRun `bdt pr status --wait` to watch the PR's CI.")


def set_draft(gh: GitHubClient, pr_id: int | None = None, target: str | None = None) -> bool:
    """Convert the current branch's ready PR -- or `pr_id`'s, if given -- back to a draft.

    Returns True if it was just converted, False if it was already a draft --
    a caller announcing "converted to draft" shouldn't do so for a PR that
    already was one.
    """
    pr_id = resolve_pr_id(gh, pr_id, target)
    pr = get_pr(gh, pr_id, fields="number,isDraft,id")
    if pr.get("isDraft"):
        return False
    gh.graphql(_DRAFT_MUTATION, id=pr["id"])
    return True


def _git(args: list[str], env: dict[str, str] | None = None, *, timeout: float = CLI_TIMEOUT_SECS) -> str:
    r = _run(["git", *args], capture_output=True, encoding="utf-8", env=env, timeout=timeout)
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
                _git(["fetch", "origin", SCREENSHOTS_BRANCH], env=env, timeout=CLI_UPLOAD_TIMEOUT_SECS)
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
            timeout=CLI_UPLOAD_TIMEOUT_SECS,
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
    gh: GitHubClient,
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
    pr = _pr_rest(gh, None)
    body: str = pr["body"]
    if screenshot_paths:
        body = build_screenshots_section(body, _screenshot_images(owner, repo, branch, screenshot_paths))
    if file_paths:
        body = build_attachments_section(body, _file_links(owner, repo, branch, file_paths))
    gh.patch(f"{gh.repo_path}/pulls/{pr['number']}", {"body": body})
    print(f"Attached {len(screenshot_paths)} screenshot(s), {len(file_paths)} file(s) to PR #{pr['number']}")


def add_screenshots(gh: GitHubClient, owner: str, repo: str, branch: str, screenshot_paths: list[str]) -> None:
    """Push screenshots to the `pr-assets` branch and append them to the current branch's PR body."""
    add_attachments(gh, owner, repo, branch, screenshot_paths=screenshot_paths)


def add_files(gh: GitHubClient, owner: str, repo: str, branch: str, file_paths: list[str]) -> None:
    """Push files to the `pr-assets` branch and append them as linked attachments to the current branch's PR body."""
    add_attachments(gh, owner, repo, branch, file_paths=file_paths)


def update(
    gh: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    title: str | None = None,
    description: str | None = None,
    screenshot_paths: list[str] | None = None,
    file_paths: list[str] | None = None,
    pr_id: int | None = None,
) -> None:
    """Update a PR's title and/or body, optionally appending screenshots/files to the body.

    Acts on the current branch's PR, or on `pr_id` directly if given -- in which case
    `branch` (used only to namespace uploaded screenshots/files, see `push_assets`) is
    overridden with the PR's actual head branch rather than trusting the caller's guess,
    since there may be no matching branch checked out locally at all.
    """
    pr = _pr_rest(gh, pr_id)
    if pr_id is not None:
        branch = pr.get("headRefName") or branch
    payload: dict[str, str] = {}
    if title:
        payload["title"] = title
    if description is not None or screenshot_paths or file_paths:
        new_body: str = description if description is not None else (pr.get("body") or "")
        if screenshot_paths:
            new_body = build_screenshots_section(new_body, _screenshot_images(owner, repo, branch, screenshot_paths))
        if file_paths:
            new_body = build_attachments_section(new_body, _file_links(owner, repo, branch, file_paths))
        new_body = ensure_agent_session_note(new_body, also_check=title or pr.get("title")) or ""
        payload["body"] = new_body
    if not payload:
        return
    gh.patch(f"{gh.repo_path}/pulls/{pr['number']}", payload)
    print(f"Updated PR #{pr['number']}")


def comment_with_screenshots(
    gh: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    message: str | None,
    screenshot_paths: list[str],
    file_paths: list[str] | None = None,
    pr_id: int | None = None,
) -> None:
    """Post a comment, with a message and/or screenshots/files, on the current branch's PR --
    or on `pr_id` directly, if given.
    """
    file_paths = file_paths or []
    images = _screenshot_images(owner, repo, branch, screenshot_paths) if screenshot_paths else []
    files = _file_links(owner, repo, branch, file_paths) if file_paths else []
    content = ensure_agent_session_note(build_comment_content(message, images, files)) or ""
    gh.post(f"{gh.repo_path}/issues/{_pr_number(gh, pr_id)}/comments", {"body": content})
    target = f"PR #{pr_id}" if pr_id is not None else "the current PR"
    print(f"Added comment ({len(screenshot_paths)} screenshot(s), {len(file_paths)} file(s)) to {target}")


def protection_requires_status_checks(protection: dict) -> bool:
    """True if a branch's `.../protection` response requires status checks to pass."""
    return bool(protection.get("required_status_checks"))


def has_build_policy(gh: GitHubClient, branch: str) -> bool:
    """Best-effort check for whether `branch` has branch protection requiring status checks.

    Only used to decide whether to print a `bdt pr status` reminder after
    `pr create` -- any failure (no permission to read protection settings, branch
    not protected at all, a timed-out call, etc.) fails open (returns False)
    rather than blocking PR creation.
    """
    try:
        data = gh.get(f"{gh.repo_path}/branches/{quote(branch, safe='/')}/protection")
    except (SystemExit, ValueError):
        return False
    return isinstance(data, dict) and protection_requires_status_checks(data)
