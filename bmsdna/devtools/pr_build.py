"""Azure DevOps PR build status: find the PR for the current branch and report its builds."""

from __future__ import annotations

import re
import sys
import time
from urllib.parse import quote

import requests

from .ado_auth import auth_header
from .gitrepo import AdoRemote, current_branch

# Matches an ISO 8601 timestamp at the start of a log line, e.g. 2024-03-21T15:01:23.1234567Z
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s*")


def _base_url(remote: AdoRemote) -> str:
    return f"https://dev.azure.com/{remote.org}/{quote(remote.project, safe='')}"


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
            return items[0]

    print(f"No PR found from '{source_branch}' → '{target_branch}'")
    sys.exit(1)


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
