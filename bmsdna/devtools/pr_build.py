"""Azure DevOps PR build status: find the PR for the current branch and report its builds."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

from .ado_auth import auth_header
from .gitrepo import AdoRemote, current_branch
from .pr_markdown import build_screenshots_section

# Matches an ISO 8601 timestamp at the start of a log line, e.g. 2024-03-21T15:01:23.1234567Z
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s*")

# GitPullRequest.mergeStatus values (PullRequestAsyncStatus) that mean the PR
# can't be merged as-is — build status is moot until this is resolved.
BAD_MERGE_STATUSES = {
    "conflicts": "has merge conflicts with the target branch",
    "failure": "merge failed",
    "rejectedByPolicy": "merge was rejected by branch policy",
}


def _base_url(remote: AdoRemote) -> str:
    return f"https://dev.azure.com/{remote.org}/{quote(remote.project, safe='')}"


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
        )
        r.raise_for_status()
        repo = r.json()

        r = session.get(f"{_base_url(remote)}/_apis/policy/configurations", params={"api-version": "7.1"})
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
    )
    r.raise_for_status()
    return r.json()["url"]


def _upload_screenshots(session: requests.Session, remote: AdoRemote, pr_id: int, screenshot_paths: list[str]) -> list[tuple[str, str]]:
    """Upload each screenshot as a PR attachment; returns (display name, url) pairs.

    Attachment names are index-prefixed so two screenshots sharing a basename
    (e.g. two 'before.png' from different folders) don't overwrite each other.
    """
    return [
        (Path(path).name, upload_attachment(session, remote, pr_id, f"{i:02d}-{Path(path).name}", path))
        for i, path in enumerate(screenshot_paths)
    ]


def _patch_pr(session: requests.Session, remote: AdoRemote, pr_id: int, fields: dict) -> None:
    r = session.patch(
        f"{_base_url(remote)}/_apis/git/repositories/{quote(remote.repo, safe='')}/pullRequests/{pr_id}",
        params={"api-version": "7.1"},
        json=fields,
    )
    r.raise_for_status()


def add_screenshots(session: requests.Session, remote: AdoRemote, pr: dict, screenshot_paths: list[str]) -> None:
    """Upload each screenshot as a PR attachment and append them to the PR description."""
    pr_id = pr["pullRequestId"]
    images = _upload_screenshots(session, remote, pr_id, screenshot_paths)
    _patch_pr(session, remote, pr_id, {"description": build_screenshots_section(pr.get("description"), images)})
    print(f"Attached {len(screenshot_paths)} screenshot(s) to PR #{pr_id}")


def update(
    session: requests.Session,
    remote: AdoRemote,
    pr: dict,
    title: str | None = None,
    description: str | None = None,
    screenshot_paths: list[str] | None = None,
) -> None:
    """Update a PR's title and/or description, optionally appending screenshots to the description."""
    pr_id = pr["pullRequestId"]
    fields: dict = {}
    if title:
        fields["title"] = title
    if description is not None or screenshot_paths:
        new_description = description if description is not None else pr.get("description")
        if screenshot_paths:
            new_description = build_screenshots_section(new_description, _upload_screenshots(session, remote, pr_id, screenshot_paths))
        fields["description"] = new_description
    if not fields:
        return
    _patch_pr(session, remote, pr_id, fields)
    print(f"Updated PR #{pr_id}")


def add_comment(session: requests.Session, remote: AdoRemote, pr_id: int, content: str) -> None:
    """Post a new top-level comment thread on the PR."""
    r = session.post(
        f"{_base_url(remote)}/_apis/git/repositories/{quote(remote.repo, safe='')}/pullRequests/{pr_id}/threads",
        params={"api-version": "7.1"},
        json={"comments": [{"parentCommentId": 0, "content": content, "commentType": 1}], "status": 1},
    )
    r.raise_for_status()


def comment_with_screenshots(session: requests.Session, remote: AdoRemote, pr_id: int, message: str | None, screenshot_paths: list[str]) -> None:
    """Post a comment, with a message and/or screenshots, on the PR."""
    images = _upload_screenshots(session, remote, pr_id, screenshot_paths) if screenshot_paths else []
    content = build_screenshots_section(message, images).strip() if images else (message or "")
    add_comment(session, remote, pr_id, content)
    print(f"Added comment ({len(screenshot_paths)} screenshot(s)) to PR #{pr_id}")


def get_builds_for_pr(session: requests.Session, remote: AdoRemote, source_branch: str, pr_id: int) -> list:
    builds = []
    for ref in [f"refs/pull/{pr_id}/merge", f"refs/heads/{source_branch}"]:
        url = f"{_base_url(remote)}/_apis/build/builds"
        r = session.get(url, params={"branchName": ref, "$top": 5, "api-version": "7.1"})
        r.raise_for_status()
        builds.extend(r.json().get("value", []))

    if builds:
        builds.sort(key=lambda b: b["id"], reverse=True)
    return builds


def latest_per_pipeline(builds: list) -> list:
    """Reduce a build list to the single latest build per pipeline (definition)."""
    latest: dict = {}
    for b in builds:
        def_id = b.get("definition", {}).get("id")
        if def_id not in latest or b["id"] > latest[def_id]["id"]:
            latest[def_id] = b
    return sorted(latest.values(), key=lambda b: b["id"], reverse=True)


def get_failed_step_logs(session: requests.Session, remote: AdoRemote, build_id: int) -> None:
    r = session.get(f"{_base_url(remote)}/_apis/build/builds/{build_id}/timeline", params={"api-version": "7.1"})
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
        r2 = session.get(log_url, params={"api-version": "7.1"})
        r2.raise_for_status()
        for line in r2.text.splitlines():
            print(f"    {TIMESTAMP_RE.sub('', line)}")


def print_build(session: requests.Session, remote: AdoRemote, build: dict) -> None:
    build_id = build["id"]
    status = build.get("status", "unknown")
    result = build.get("result", "—")
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


def run(remote: AdoRemote, pat: str | None, target_branch: str, wait: bool, source_branch: str | None = None) -> None:
    source_branch = source_branch or current_branch()
    session = requests.Session()
    session.headers.update(auth_header(pat))

    # When waiting, a pipeline's "latest" build may already be a completed run
    # from before this invocation. Only accept builds newer than whatever was
    # already there when we started, so --wait actually waits for the build(s)
    # triggered by the current HEAD instead of immediately reporting a stale result.
    baseline_ids: dict[int, int] = {}
    if wait:
        pr = get_pr(session, remote, source_branch, target_branch)
        for b in get_builds_for_pr(session, remote, source_branch, pr["pullRequestId"]):
            def_id = b.get("definition", {}).get("id")
            baseline_ids[def_id] = max(baseline_ids.get(def_id, 0), b["id"])

    last_line = ""
    while True:
        pr = get_pr(session, remote, source_branch, target_branch)
        pr_id = pr["pullRequestId"]
        pr_title = pr.get("title", "?")
        pr_status = pr.get("status", "?")

        msg = f"\rPR #{pr_id}: {pr_title} ({pr_status})"

        builds = get_builds_for_pr(session, remote, source_branch, pr_id)
        if builds:
            pipeline_builds = latest_per_pipeline(builds)
            if wait:
                stale = [b for b in pipeline_builds if b["id"] <= baseline_ids.get(b.get("definition", {}).get("id"), 0)]
                if stale:
                    msg += " | waiting for new build(s) to start: " + ", ".join(
                        b.get("definition", {}).get("name", "?") for b in stale
                    )
                    if msg != last_line:
                        print(msg, end="", flush=True)
                        last_line = msg
                    time.sleep(30)
                    continue
            msg += " | " + ", ".join(
                f"{b.get('definition', {}).get('name', '?')} #{b['id']} {b.get('status')} ({b.get('result', '—')})"
                for b in pipeline_builds
            )

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
        else:
            msg += " | No builds found."
            if not wait:
                print(msg)
                return

        if msg != last_line:
            print(msg, end="", flush=True)
            last_line = msg

        if wait:
            time.sleep(30)
