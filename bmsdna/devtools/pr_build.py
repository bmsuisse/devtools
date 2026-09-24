"""Azure DevOps PR build status: find the PR for the current branch and report its builds."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

from .ado_auth import auth_header
from .cli_tools import EXIT_NEEDS_APPROVAL, HTTP_TIMEOUT_SECS, PollHeartbeat, detect_agent_session, ensure_agent_session_note, is_claude_code
from .gitrepo import AdoRemote, current_branch
from .pr_markdown import build_attachments_section, build_comment_content, build_screenshots_section

# Matches an ISO 8601 timestamp at the start of a log line, e.g. 2024-03-21T15:01:23.1234567Z
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s*")

# Timeline record name for a YAML pipeline stage's manual-approval check. Its `state` stays
# "inProgress" (like an ordinary running step) until someone approves/rejects it or it times
# out — indistinguishable from "still building" unless you look at the timeline specifically.
CHECKPOINT_APPROVAL_NAME = "Checkpoint.Approval"

# `HTTP_TIMEOUT_SECS` is generous for a plain JSON GET/PATCH, but too tight for uploading a
# whole file's bytes (a screenshot/attachment) -- give those more room instead of failing an
# otherwise-fine upload just because it's slower than a metadata call.
HTTP_UPLOAD_TIMEOUT_SECS = HTTP_TIMEOUT_SECS * 4

# GitPullRequest.mergeStatus values (PullRequestAsyncStatus) that mean the PR
# can't be merged as-is — build status is moot until this is resolved.
BAD_MERGE_STATUSES = {
    "conflicts": "has merge conflicts with the target branch",
    "failure": "merge failed",
    "rejectedByPolicy": "merge was rejected by branch policy",
}


def _base_url(remote: AdoRemote) -> str:
    return f"https://dev.azure.com/{remote.org}/{quote(remote.project, safe='')}"


def _poll_or_exit(fn, *args, **kwargs):
    """Calls `fn(*args, **kwargs)`, converting a network/timeout failure into a clear exit
    message instead of an uncaught `requests` traceback -- for the calls a `--wait` poll
    loop (`run`/`run_watch_deploy`) makes every 30s, where a bare `raise` would otherwise
    surface as a raw stack trace the first time a request stalls or the connection drops.

    Deliberately not baked into `get_pr`/`get_builds_for_pr`/`get_builds_for_branch`
    themselves -- several best-effort callers (`has_build_policy`, `deploy_build_hint`,
    `find_pending_approvals`) already wrap those in their own `except requests.RequestException`
    to fail open, and converting the exception to `SystemExit` at the source would break that.
    """
    try:
        return fn(*args, **kwargs)
    except requests.exceptions.RequestException as e:
        sys.exit(f"Azure DevOps request failed or timed out: {e}")


def pr_web_url(remote: AdoRemote, pr_id: int) -> str:
    """The browsable web page for a PR, as opposed to its REST API URL (which is all the
    `az repos pr create` JSON response otherwise gives you).
    """
    return f"{_base_url(remote)}/_git/{quote(remote.repo, safe='')}/pullrequest/{pr_id}"


# PolicyType.id for the built-in "Build" policy (branch policy requiring a
# build to pass) — same GUID across every ADO organization.
BUILD_POLICY_TYPE_ID = "0609b952-1397-4640-95ec-e00a01b2c241"


def _scope_matches(scope: dict, repo_id: str, target_ref: str, default_branch: str | None) -> bool:
    if scope.get("repositoryId") not in (None, repo_id):
        return False
    if scope.get("matchKind") == "DefaultBranch":
        return default_branch == target_ref
    return scope.get("refName") in (None, target_ref)


def policy_configs_include_branch(configs: list, repo_id: str, branch: str, default_branch: str | None) -> bool:
    """True if any enabled, non-deleted Build-type policy configuration's scope covers `branch`."""
    target_ref = f"refs/heads/{branch}"
    for config in configs:
        if not config.get("isEnabled") or config.get("isDeleted"):
            continue
        if config.get("type", {}).get("id") != BUILD_POLICY_TYPE_ID:
            continue
        scopes = config.get("settings", {}).get("scope", [])
        if any(_scope_matches(scope, repo_id, target_ref, default_branch) for scope in scopes):
            return True
    return False


def has_build_policy(session: requests.Session, remote: AdoRemote, branch: str) -> bool:
    """Best-effort check for whether `branch` has an enabled Build policy configured.

    Only used to decide whether to print a `bdt pr status` reminder after
    `pr create` — failures here (auth, permissions, network) fail open
    (return False) rather than blocking PR creation.
    """
    try:
        r = session.get(
            f"{_base_url(remote)}/_apis/git/repositories/{quote(remote.repo, safe='')}",
            params={"api-version": "7.1"},
            timeout=HTTP_TIMEOUT_SECS,
        )
        r.raise_for_status()
        repo = r.json()

        r = session.get(f"{_base_url(remote)}/_apis/policy/configurations", params={"api-version": "7.1"}, timeout=HTTP_TIMEOUT_SECS)
        r.raise_for_status()
        configs = r.json().get("value", [])
    except (requests.RequestException, SystemExit):
        return False

    return policy_configs_include_branch(configs, repo.get("id"), branch, repo.get("defaultBranch"))


def merge_conflict_message(pr: dict) -> str | None:
    """None if the PR's mergeStatus is fine; else a human-readable description of the problem."""
    merge_status = pr.get("mergeStatus")
    if merge_status not in BAD_MERGE_STATUSES:
        return None
    pr_id = pr.get("pullRequestId")
    title = pr.get("title", "?")
    detail = pr.get("mergeFailureMessage") or BAD_MERGE_STATUSES[merge_status]
    return f"PR #{pr_id} ({title!r}) {detail} (mergeStatus={merge_status})"


def draft_notice(pr: dict) -> str | None:
    """None if the PR isn't a draft; else a heads-up that it is.

    Purely informational, not an error — review typically already happened before
    `pr status` is even run, so this doesn't block the rest of the command.
    """
    if not pr.get("isDraft"):
        return None
    pr_ref = f"PR #{pr.get('pullRequestId')} is a draft - no CI yet."
    if is_claude_code():
        return f"{pr_ref} Run `/code-review` first, then `bdt pr publish`."
    return f"{pr_ref} To publish, use command: `bdt pr publish` (but do an automatic code review first)"


def retry_hint() -> str:
    """The command to tell someone to run after a failed build, to retry just its failed
    stage(s)/job(s) instead of queuing a whole new build.
    """
    if is_claude_code():
        return "Run `bdt pr retry` to retry just the failed stage(s)/job(s)."
    return "Hint: to retry just the failed stage(s)/job(s) instead of queuing a full rerun, run: `bdt pr retry`"


def get_pr(session: requests.Session, remote: AdoRemote, source_branch: str, target_branch: str) -> dict:
    url = f"{_base_url(remote)}/_apis/git/repositories/{remote.repo}/pullrequests"
    for status in ["active", "completed"]:
        r = session.get(
            url,
            params={
                "searchCriteria.sourceRefName": f"refs/heads/{source_branch}",
                "searchCriteria.targetRefName": f"refs/heads/{target_branch}",
                "searchCriteria.status": status,
                "$top": 1,
                "api-version": "7.1",
            },
            timeout=HTTP_TIMEOUT_SECS,
        )
        r.raise_for_status()
        items = r.json().get("value", [])
        if items:
            pr = items[0]
            conflict = merge_conflict_message(pr)
            if conflict:
                sys.exit(conflict)
            return pr

    print(f"No PR found from '{source_branch}' → '{target_branch}'")
    sys.exit(1)


def upload_attachment(session: requests.Session, remote: AdoRemote, pr_id: int, attachment_name: str, file_path: str) -> str:
    """Upload `file_path` as a pull request attachment named `attachment_name`; returns its download URL.

    Embedding that URL in the PR description works because the browser
    request for the image is same-origin (dev.azure.com) and carries the
    viewer's own auth session/cookies — no separate hosting needed.
    """
    r = session.post(
        f"{_base_url(remote)}/_apis/git/repositories/{quote(remote.repo, safe='')}"
        f"/pullRequests/{pr_id}/attachments/{quote(attachment_name, safe='')}",
        params={"api-version": "7.1"},
        data=Path(file_path).read_bytes(),
        headers={"Content-Type": "application/octet-stream"},
        timeout=HTTP_UPLOAD_TIMEOUT_SECS,
    )
    r.raise_for_status()
    return r.json()["url"]


def _upload_attachments(session: requests.Session, remote: AdoRemote, pr_id: int, paths: list[str]) -> list[tuple[str, str]]:
    """Upload each path as a PR attachment (screenshot or arbitrary file); returns (display name, url) pairs.

    Attachment names are index-prefixed so two paths sharing a basename (e.g. two 'before.png'
    from different folders) don't overwrite each other.
    """
    return [
        (Path(path).name, upload_attachment(session, remote, pr_id, f"{i:02d}-{Path(path).name}", path))
        for i, path in enumerate(paths)
    ]


def _patch_pr(session: requests.Session, remote: AdoRemote, pr_id: int, fields: dict) -> None:
    r = session.patch(
        f"{_base_url(remote)}/_apis/git/repositories/{quote(remote.repo, safe='')}/pullRequests/{pr_id}",
        params={"api-version": "7.1"},
        json=fields,
        timeout=HTTP_TIMEOUT_SECS,
    )
    r.raise_for_status()


def add_attachments(
    session: requests.Session,
    remote: AdoRemote,
    pr: dict,
    screenshot_paths: list[str] | None = None,
    file_paths: list[str] | None = None,
) -> dict:
    """Upload screenshots/files as PR attachments and append them to the PR description in a single
    patch -- whether one or both kinds are given. Returns `pr` with its description updated to match,
    so a caller chaining another description-touching step (e.g. `ensure_session_note`) right after
    doesn't need to re-fetch the PR just to see this change.
    """
    screenshot_paths = screenshot_paths or []
    file_paths = file_paths or []
    pr_id = pr["pullRequestId"]
    new_description = pr.get("description")
    if screenshot_paths:
        new_description = build_screenshots_section(new_description, _upload_attachments(session, remote, pr_id, screenshot_paths))
    if file_paths:
        new_description = build_attachments_section(new_description, _upload_attachments(session, remote, pr_id, file_paths))
    _patch_pr(session, remote, pr_id, {"description": new_description})
    print(f"Attached {len(screenshot_paths)} screenshot(s), {len(file_paths)} file(s) to PR #{pr_id}")
    return {**pr, "description": new_description}


def ensure_session_note(session: requests.Session, remote: AdoRemote, pr: dict) -> None:
    """Make sure the PR description records the running agent's session, if any (see
    `ensure_agent_session_note`) -- e.g. right after `az repos pr create`, whose
    description usually comes from the source commit rather than a value this module
    builds itself, so there's nothing to pass the note through beforehand.

    Best-effort: swallows failures (auth, permissions, a stale `pr`, ...) rather than
    turning a successful PR creation into a failure over a step that's purely
    nice-to-have. A no-op when no agent is detected, which is the common case
    running outside one -- callers should still avoid fetching `pr` at all in that
    case (see `detect_agent_session`) rather than relying on this to skip the patch.
    """
    if detect_agent_session() is None:
        return
    try:
        pr_id = pr["pullRequestId"]
        body = pr.get("description") or ""
        noted = ensure_agent_session_note(body, also_check=pr.get("title"))
        if noted != body:
            _patch_pr(session, remote, pr_id, {"description": noted})
    except (requests.RequestException, SystemExit, KeyError):
        pass


def add_screenshots(session: requests.Session, remote: AdoRemote, pr: dict, screenshot_paths: list[str]) -> None:
    """Upload each screenshot as a PR attachment and append them to the PR description."""
    add_attachments(session, remote, pr, screenshot_paths=screenshot_paths)


def add_files(session: requests.Session, remote: AdoRemote, pr: dict, file_paths: list[str]) -> None:
    """Upload each file as a PR attachment and append them as linked attachments to the PR description."""
    add_attachments(session, remote, pr, file_paths=file_paths)


def update(
    session: requests.Session,
    remote: AdoRemote,
    pr: dict,
    title: str | None = None,
    description: str | None = None,
    screenshot_paths: list[str] | None = None,
    file_paths: list[str] | None = None,
) -> None:
    """Update a PR's title and/or description, optionally appending screenshots/files to the description."""
    pr_id = pr["pullRequestId"]
    fields: dict = {}
    if title:
        fields["title"] = title
    if description is not None or screenshot_paths or file_paths:
        new_description = description if description is not None else pr.get("description")
        if screenshot_paths:
            new_description = build_screenshots_section(new_description, _upload_attachments(session, remote, pr_id, screenshot_paths))
        if file_paths:
            new_description = build_attachments_section(new_description, _upload_attachments(session, remote, pr_id, file_paths))
        fields["description"] = ensure_agent_session_note(new_description, also_check=title or pr.get("title"))
    if not fields:
        return
    _patch_pr(session, remote, pr_id, fields)
    print(f"Updated PR #{pr_id}")


def publish(session: requests.Session, remote: AdoRemote, pr: dict) -> None:
    """Mark a draft PR as ready for review (clears isDraft)."""
    pr_id = pr["pullRequestId"]
    _patch_pr(session, remote, pr_id, {"isDraft": False})
    print(f"Marked PR #{pr_id} as ready for review")
    print("\nRun `bdt pr status --wait` to watch the PR's CI.")


def set_draft(session: requests.Session, remote: AdoRemote, pr: dict) -> bool:
    """Convert a ready PR back to a draft (sets isDraft).

    Returns True if it was just converted, False if it was already a draft --
    a caller announcing "converted to draft" shouldn't do so for a PR that
    already was one.
    """
    if pr.get("isDraft"):
        return False
    _patch_pr(session, remote, pr["pullRequestId"], {"isDraft": True})
    return True


def add_comment(session: requests.Session, remote: AdoRemote, pr_id: int, content: str) -> None:
    """Post a new top-level comment thread on the PR."""
    r = session.post(
        f"{_base_url(remote)}/_apis/git/repositories/{quote(remote.repo, safe='')}/pullRequests/{pr_id}/threads",
        params={"api-version": "7.1"},
        json={"comments": [{"parentCommentId": 0, "content": content, "commentType": 1}], "status": 1},
        timeout=HTTP_TIMEOUT_SECS,
    )
    r.raise_for_status()


def comment_with_screenshots(
    session: requests.Session,
    remote: AdoRemote,
    pr_id: int,
    message: str | None,
    screenshot_paths: list[str],
    file_paths: list[str] | None = None,
) -> None:
    """Post a comment, with a message and/or screenshots/files, on the PR."""
    file_paths = file_paths or []
    images = _upload_attachments(session, remote, pr_id, screenshot_paths) if screenshot_paths else []
    files = _upload_attachments(session, remote, pr_id, file_paths) if file_paths else []
    content = ensure_agent_session_note(build_comment_content(message, images, files)) or ""
    add_comment(session, remote, pr_id, content)
    print(f"Added comment ({len(screenshot_paths)} screenshot(s), {len(file_paths)} file(s)) to PR #{pr_id}")


def get_builds_for_pr(session: requests.Session, remote: AdoRemote, source_branch: str, pr_id: int) -> list:
    builds = []
    for ref in [f"refs/pull/{pr_id}/merge", f"refs/heads/{source_branch}"]:
        url = f"{_base_url(remote)}/_apis/build/builds"
        r = session.get(url, params={"branchName": ref, "$top": 5, "api-version": "7.1"}, timeout=HTTP_TIMEOUT_SECS)
        r.raise_for_status()
        builds.extend(r.json().get("value", []))

    if builds:
        builds.sort(key=lambda b: b["id"], reverse=True)
    return builds


def get_builds_for_branch(session: requests.Session, remote: AdoRemote, branch: str, top: int = 5) -> list:
    """Builds triggered directly on `branch` -- e.g. a post-merge/deployment pipeline that
    only runs on the target branch once a PR merges into it -- as opposed to
    `get_builds_for_pr`, which looks at the PR's own merge/source refs. Same shape of call,
    just a different `branchName` ref.
    """
    url = f"{_base_url(remote)}/_apis/build/builds"
    r = session.get(url, params={"branchName": f"refs/heads/{branch}", "$top": top, "api-version": "7.1"}, timeout=HTTP_TIMEOUT_SECS)
    r.raise_for_status()
    builds = r.json().get("value", [])
    builds.sort(key=lambda b: b["id"], reverse=True)
    return builds


def deploy_build_hint(session: requests.Session, remote: AdoRemote, target_branch: str) -> str | None:
    """Best-effort: None unless a build has already been triggered directly on `target_branch`
    (as opposed to this PR's own merge/source refs) -- e.g. a post-merge pipeline that deploys.
    When one exists, a hint suggesting `bdt pr watch-deploy` to watch it.

    Only used to decide whether to print this hint after `bdt pr status` reports success --
    failures here (auth, permissions, network) fail open (return None) rather than blocking
    or crashing `pr status` over a step that's purely nice-to-have, same as `has_build_policy`.
    """
    try:
        builds = get_builds_for_branch(session, remote, target_branch, top=1)
    except requests.RequestException:
        return None
    if not builds:
        return None
    build = builds[0]
    name = build.get("definition", {}).get("name", "?")
    return (
        f"\nA build has already been triggered on '{target_branch}' ({name} #{build['id']}) -- "
        f"probably a post-merge deployment. Run `bdt pr watch-deploy --target-branch {target_branch}` to watch it."
    )


def latest_per_pipeline(builds: list) -> list:
    """Reduce a build list to the single latest build per pipeline (definition)."""
    latest: dict = {}
    for b in builds:
        def_id = b.get("definition", {}).get("id")
        if def_id not in latest or b["id"] > latest[def_id]["id"]:
            latest[def_id] = b
    return sorted(latest.values(), key=lambda b: b["id"], reverse=True)


def build_web_url(remote: AdoRemote, build_id: int) -> str:
    """The browsable web page for a build, where a pending approval can actually be acted on."""
    return f"{_base_url(remote)}/_build/results?buildId={build_id}&view=results"


def get_timeline_records(session: requests.Session, remote: AdoRemote, build_id: int) -> list:
    r = session.get(f"{_base_url(remote)}/_apis/build/builds/{build_id}/timeline", params={"api-version": "7.1"}, timeout=HTTP_TIMEOUT_SECS)
    r.raise_for_status()
    return r.json().get("records") or []


def pending_approval_records(records: list) -> list:
    """Timeline records for still-open `Checkpoint.Approval` gates (manual stage approvals)."""
    return [rec for rec in records if rec.get("state") == "inProgress" and rec.get("name") == CHECKPOINT_APPROVAL_NAME]


def approval_stage_name(records: list, approval_record: dict) -> str:
    """Human-readable stage name for an approval record.

    Resolved by walking the timeline's parent chain: Checkpoint.Approval -> Checkpoint -> Stage.
    Falls back to the approval record's own name if that chain is missing (unexpected shape).
    """
    by_id = {rec.get("id"): rec for rec in records}
    checkpoint = by_id.get(approval_record.get("parentId"))
    stage = by_id.get(checkpoint.get("parentId")) if checkpoint else None
    return (stage or {}).get("name") or approval_record.get("name") or "?"


def find_pending_approvals(session: requests.Session, remote: AdoRemote, builds: list) -> list:
    """(build, timeline records, pending approval records) for each build that's actually
    blocked on a stage approval, not just still running.

    Only builds with status "inProgress" have a timeline at all — one that's "notStarted"
    (queued, waiting on agent capacity) or "postponed" gets a 404 from the timeline endpoint,
    and a Checkpoint.Approval gate can only exist mid-run anyway. A build can also briefly
    report "inProgress" before its timeline document exists yet -- that 404 (like any other
    request failure here) fails open rather than crashing the --wait loop over it; the next
    poll, 30s later, tries again.
    """
    result = []
    for build in builds:
        if build.get("status") != "inProgress":
            continue
        try:
            records = get_timeline_records(session, remote, build["id"])
        except requests.RequestException:
            continue
        approvals = pending_approval_records(records)
        if approvals:
            result.append((build, records, approvals))
    return result


def get_failed_step_logs(session: requests.Session, remote: AdoRemote, build_id: int) -> None:
    r = session.get(f"{_base_url(remote)}/_apis/build/builds/{build_id}/timeline", params={"api-version": "7.1"}, timeout=HTTP_TIMEOUT_SECS)
    r.raise_for_status()
    records = r.json().get("records", [])

    failed = [rec for rec in records if rec.get("result") == "failed" and rec.get("type") == "Task" and rec.get("log")]

    if not failed:
        print("  (no failed steps with logs)")
        return

    print(f"\n--- Failed steps (build {build_id}) ---")
    for rec in failed:
        name = rec.get("name", "?")
        log_url = rec["log"]["url"]
        print(f"\n  [FAILED] {name}")
        r2 = session.get(log_url, params={"api-version": "7.1"}, timeout=HTTP_TIMEOUT_SECS)
        r2.raise_for_status()
        for line in r2.text.splitlines():
            print(f"    {TIMESTAMP_RE.sub('', line)}")


def retry_failed_build(session: requests.Session, remote: AdoRemote, build_id: int) -> None:
    """Retry only the failed stage(s)/job(s) of a completed build, in place -- distinct
    from queuing a brand new build via `Builds - Queue`.

    Uses the `retry=true` query parameter documented on Azure DevOps' "Builds - Update
    Build" REST API (PATCH .../_apis/build/builds/{buildId}?retry=true&api-version=7.1
    -- https://learn.microsoft.com/rest/api/azure/devops/build/builds/update-build).
    Azure DevOps reschedules whichever stages/jobs failed on the previous attempt (plus
    anything depending on them); stages that already succeeded are left alone. Needs a
    PAT (or `az` login) with build_execute scope, same as everything else in this module.
    """
    r = session.patch(
        f"{_base_url(remote)}/_apis/build/builds/{build_id}",
        params={"retry": "true", "api-version": "7.1"},
        json={},
        timeout=HTTP_TIMEOUT_SECS,
    )
    r.raise_for_status()


def retry(remote: AdoRemote, pat: str | None, target_branch: str, source_branch: str | None = None) -> None:
    """Retry the failed stage(s)/job(s) of the most recent build(s) for the PR opened
    from the current branch -- one retry call per pipeline that failed, without queuing
    any brand new builds.
    """
    source_branch = source_branch or current_branch()
    session = requests.Session()
    session.headers.update(auth_header(pat))

    pr = get_pr(session, remote, source_branch, target_branch)
    builds = get_builds_for_pr(session, remote, source_branch, pr["pullRequestId"])
    if not builds:
        sys.exit(f"No builds found for PR #{pr['pullRequestId']} -- nothing to retry.")

    failed = [b for b in latest_per_pipeline(builds) if b.get("status") == "completed" and b.get("result") == "failed"]
    if not failed:
        sys.exit(f"No failed builds to retry for PR #{pr['pullRequestId']}.")

    # Keep going through every failed pipeline even if one retry call fails -- a
    # transient error retrying one build shouldn't abandon retrying the others, and
    # the failure summary at the end still surfaces it.
    errors: list[str] = []
    for b in failed:
        name = b.get("definition", {}).get("name", "?")
        try:
            retry_failed_build(session, remote, b["id"])
        except requests.RequestException as e:
            errors.append(f"build #{b['id']} ({name}): {e}")
            continue
        print(f"Retrying failed stage(s)/job(s) of build #{b['id']} ({name})")

    if errors:
        sys.exit("Failed to retry: " + "; ".join(errors))

    print("\nRun `bdt pr status --wait` to watch the retry.")


def print_build(session: requests.Session, remote: AdoRemote, build: dict) -> None:
    build_id = build["id"]
    status = build.get("status", "unknown")
    # ADO's build resource always includes a `result` key, explicitly `null` (-> None) until
    # the build completes — `.get(..., "—")`'s default only covers a missing key, not this.
    result = build.get("result") or "—"
    name = build.get("definition", {}).get("name", "?")
    number = build.get("buildNumber", "?")
    start = build.get("startTime", "?")
    finish = build.get("finishTime", "?")
    source_version = build.get("sourceVersion", "?")[:8]

    icon = {"succeeded": "✓", "failed": "✗", "canceled": "⊘"}.get(result, "…")

    print(f"\n{'-' * 60}")
    print(f"Build #{build_id}  [{icon} {result.upper()}]")
    print(f"  Commit   : {source_version}")
    print(f"  Pipeline : {name}")
    print(f"  Number   : {number}")
    print(f"  Status   : {status}")
    print(f"  Started  : {start}")
    print(f"  Finished : {finish}")

    if status == "completed" and result == "failed":
        get_failed_step_logs(session, remote, build_id)


def exit_if_blocked_on_approval(
    session: requests.Session,
    remote: AdoRemote,
    msg: str,
    pipeline_builds: list,
    wait: bool,
    rerun_cmd: str,
    show_retry_hint: bool = False,
) -> None:
    """If any pipeline is blocked on a stage approval, print details and exit -- it never
    resolves on its own, so --wait must stop instead of polling forever. If another pipeline
    has already failed, that's the more urgent, more actionable fact: report it (exit 1, not
    the approval code) instead of just telling the user to go approve a stage while staying
    unaware CI already failed elsewhere -- with the same `retry_hint()` a plain failure report
    would get, when `show_retry_hint` says that applies here (it doesn't for `watch-deploy`,
    where `bdt pr retry` has nothing to act on -- there's no PR whose checks it retries).
    A no-op (returns normally) if nothing is blocked.
    """
    already_failed = any(b.get("result") == "failed" for b in pipeline_builds)
    pending_approvals = find_pending_approvals(session, remote, pipeline_builds) if wait else []
    if not pending_approvals:
        return
    print(msg)
    if already_failed:
        print("\nNote: another pipeline has already failed — see details below.")
    print("\nWaiting for approval:")
    for build, records, approvals in pending_approvals:
        pipeline_name = build.get("definition", {}).get("name", "?")
        for rec in approvals:
            stage = approval_stage_name(records, rec)
            print(f"  {pipeline_name} #{build['id']}: stage '{stage}' needs approval — {build_web_url(remote, build['id'])}")
    if already_failed:
        print("\nDetails:")
        for b in pipeline_builds:
            print_build(session, remote, b)
        if show_retry_hint:
            print(f"\n{retry_hint()}")
        sys.exit(1)
    print(f"\nApprove at the link(s) above, then re-run `{rerun_cmd}`.")
    sys.exit(EXIT_NEEDS_APPROVAL)


def run(remote: AdoRemote, pat: str | None, target_branch: str, wait: bool, source_branch: str | None = None) -> None:
    source_branch = source_branch or current_branch()
    session = requests.Session()
    session.headers.update(auth_header(pat))

    # When waiting, a pipeline's "latest" build may already be a *completed* run from before
    # this invocation (CI hasn't registered a new build for the current push yet). Only accept
    # a fresh build in that case, so --wait doesn't immediately report that stale old result.
    # Only builds already completed at this snapshot go in here -- one that's inProgress here
    # (whether just-started or long-running) is genuinely the current build for the current
    # HEAD, not a stale leftover, and must be watched rather than waited past: recording it too
    # would make the loop below treat "still the same build, same id" as "stale" forever,
    # since a running build keeps the same id for its whole life -- reintroducing the hang
    # this function exists to avoid, for any --wait invoked after the build had already started.
    baseline_completed_ids: dict[int, int] = {}
    if wait:
        pr = _poll_or_exit(get_pr, session, remote, source_branch, target_branch)
        for b in _poll_or_exit(get_builds_for_pr, session, remote, source_branch, pr["pullRequestId"]):
            if b.get("status") != "completed":
                continue
            def_id = b.get("definition", {}).get("id")
            baseline_completed_ids[def_id] = max(baseline_completed_ids.get(def_id, 0), b["id"])

    draft_notice_shown = False
    heartbeat = PollHeartbeat()
    while True:
        pr = _poll_or_exit(get_pr, session, remote, source_branch, target_branch)
        if not draft_notice_shown:
            draft_msg = draft_notice(pr)
            if draft_msg:
                print(draft_msg)
            draft_notice_shown = True
        pr_id = pr["pullRequestId"]
        pr_title = pr.get("title", "?")
        pr_status = pr.get("status", "?")

        msg = f"\rPR #{pr_id}: {pr_title} ({pr_status})"

        builds = _poll_or_exit(get_builds_for_pr, session, remote, source_branch, pr_id)
        if builds:
            pipeline_builds = latest_per_pipeline(builds)
            if wait:
                stale = [b for b in pipeline_builds if b["id"] <= baseline_completed_ids.get(b.get("definition", {}).get("id"), 0)]
                if stale:
                    msg += " | waiting for new build(s) to start: " + ", ".join(
                        b.get("definition", {}).get("name", "?") for b in stale
                    )
                    heartbeat.show(msg)
                    time.sleep(30)
                    continue
            msg += " | " + ", ".join(
                f"{b.get('definition', {}).get('name', '?')} #{b['id']} {b.get('status')} ({b.get('result') or '—'})"
                for b in pipeline_builds
            )

            exit_if_blocked_on_approval(session, remote, msg, pipeline_builds, wait, "bdt pr status --wait", show_retry_hint=True)

            all_done = all(b.get("status") == "completed" for b in pipeline_builds)
            if all_done or not wait:
                print(msg)
                print("\nDetails:")
                for b in pipeline_builds:
                    print_build(session, remote, b)

                if not all_done and not wait:
                    print("\nTip: Use --wait to poll until all pipelines are completed.")

                if any(b.get("result") == "failed" for b in pipeline_builds):
                    print(f"\n{retry_hint()}")
                    sys.exit(1)
                # Only worth suggesting `pr watch-deploy` once this PR's own pipeline(s) have
                # actually finished (not just because the caller ran without --wait) --
                # otherwise it'd claim a merge/deploy is underway before the PR's own build
                # has even completed.
                if all_done:
                    hint = deploy_build_hint(session, remote, target_branch)
                    if hint:
                        print(hint)
                return
        else:
            msg += " | No builds found."
            if not wait:
                print(msg)
                # No PR-triggered builds at all is itself a settled state (e.g. this
                # project's only pipeline triggers on a push to the target branch, not
                # on the PR's own merge/source refs) -- exactly when a deploy-build hint
                # is most useful, so still check for one.
                hint = deploy_build_hint(session, remote, target_branch)
                if hint:
                    print(hint)
                return

        heartbeat.show(msg)

        if wait:
            time.sleep(30)


def run_watch_deploy(remote: AdoRemote, pat: str | None, target_branch: str, wait: bool) -> None:
    """Watch the most recent build(s) triggered directly on `target_branch` -- e.g. a
    post-merge pipeline that only runs on the target branch once a PR merges into it, and
    usually does the actual deployment -- until they complete.

    Mirrors `run()`'s polling/reporting shape (same `latest_per_pipeline`/`print_build`
    helpers), but looks at builds tied to the branch itself via `get_builds_for_branch`
    rather than a specific PR's merge/source refs.
    """
    session = requests.Session()
    session.headers.update(auth_header(pat))

    heartbeat = PollHeartbeat()
    while True:
        builds = _poll_or_exit(get_builds_for_branch, session, remote, target_branch)
        if not builds:
            print(f"No builds found on '{target_branch}'.")
            return

        pipeline_builds = latest_per_pipeline(builds)
        msg = f"\rBranch '{target_branch}' | " + ", ".join(
            f"{b.get('definition', {}).get('name', '?')} #{b['id']} {b.get('status')} ({b.get('result') or '—'})"
            for b in pipeline_builds
        )

        exit_if_blocked_on_approval(session, remote, msg, pipeline_builds, wait, "bdt pr watch-deploy --wait")

        all_done = all(b.get("status") == "completed" for b in pipeline_builds)
        if all_done or not wait:
            print(msg)
            print("\nDetails:")
            for b in pipeline_builds:
                print_build(session, remote, b)

            if not all_done and not wait:
                print("\nTip: Use --wait to poll until all pipelines are completed.")

            if any(b.get("result") == "failed" for b in pipeline_builds):
                sys.exit(1)
            return

        heartbeat.show(msg)
        time.sleep(30)
