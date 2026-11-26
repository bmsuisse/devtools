"""`bdt pr logs`: print the logs of a PR's builds (Azure DevOps) / workflow runs (GitHub).

`bdt pr status` only shows the failed steps while it reports status; this is the dedicated viewer:
choose failed steps (default) or every step, filter by name, tail, list without content.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field

import requests

from bmsdna.devtools import gh_pr, pr_build
from bmsdna.devtools.ado_auth import ado_session
from bmsdna.devtools.cli_tools import HTTP_TIMEOUT_SECS
from bmsdna.devtools.github_api import GitHubClient
from bmsdna.devtools.gitrepo import AdoRemote

NO_LOG = "(no log available)"


@dataclass
class LogSection:
    """One step (Azure DevOps) or job (GitHub) with a log. `fetch` is only called when the text is shown."""

    title: str
    result: str
    fetch: Callable[[], str] = field(repr=False, default=lambda: "")


@dataclass
class LogSource:
    """A build / workflow run and its sections."""

    header: str
    sections: list[LogSection]


def clean_log(text: str, timestamps: bool = False) -> str:
    """The log without the per-line ISO timestamps Azure Pipelines prefixes (kept with `timestamps`)."""
    lines = text.splitlines()
    if not timestamps:
        lines = [pr_build.TIMESTAMP_RE.sub("", line) for line in lines]
    return "\n".join(lines)


def tail_lines(text: str, tail: int) -> str:
    """Only the last `tail` lines (0 = everything), with a note how many were left out."""
    lines = text.splitlines()
    if tail <= 0 or len(lines) <= tail:
        return text
    return "\n".join([f"... ({len(lines) - tail} earlier lines omitted; --tail 0 shows all)", *lines[-tail:]])


def matches_step(title: str, step: str | None) -> bool:
    return step is None or step.casefold() in title.casefold()


def ado_sections(
    records: list[dict], fetch_log: Callable[[str], str], *, show_all: bool, step: str | None
) -> list[LogSection]:
    """The timeline's tasks that have a log, as `Stage / Job / Task` sections in start order.

    Without `show_all` only failed tasks; `step` keeps those whose path contains it (case-insensitive).
    """
    by_id = {rec.get("id"): rec for rec in records}

    def path(rec: dict) -> str:
        names = [rec.get("name") or "?"]
        parent = by_id.get(rec.get("parentId"))
        while parent is not None:
            if parent.get("type") in ("Stage", "Job"):
                names.append(parent.get("name") or "?")
            parent = by_id.get(parent.get("parentId"))
        return " / ".join(reversed(names))

    tasks = [r for r in records if r.get("type") == "Task" and (r.get("log") or {}).get("url")]
    tasks.sort(key=lambda r: r.get("startTime") or "")
    sections = []
    for rec in tasks:
        result = rec.get("result") or rec.get("state") or "?"
        title = path(rec)
        if (show_all or result == "failed") and matches_step(title, step):
            url = rec["log"]["url"]
            sections.append(LogSection(title, result, lambda url=url: fetch_log(url)))
    return sections


def ado_source(
    session: requests.Session, remote: AdoRemote, build: dict, *, show_all: bool, step: str | None
) -> LogSource:
    def fetch_log(url: str) -> str:
        r = session.get(url, params={"api-version": "7.1"}, timeout=HTTP_TIMEOUT_SECS)
        r.raise_for_status()
        return r.text

    pipeline = (build.get("definition") or {}).get("name", "?")
    state = build.get("result") or build.get("status") or "?"
    header = f"Build {build['id']}  {pipeline}  [{state}]\n{pr_build.build_web_url(remote, build['id'])}"
    records = pr_build.get_timeline_records(session, remote, build["id"])
    return LogSource(header, ado_sections(records, fetch_log, show_all=show_all, step=step))


def github_source(gh: GitHubClient, run_id: int, label: str | None, *, show_all: bool, step: str | None) -> LogSource:
    """A workflow run's jobs. GitHub serves one log per *job* (not per step), so a failed job's
    header names its failed steps instead."""
    run = gh.get(f"{gh.repo_path}/actions/runs/{run_id}")
    name = label or run.get("name") or "?"
    state = run.get("conclusion") or run.get("status") or "?"
    header = f"Run {run_id}  {name}  [{state}]\n{run.get('html_url', '')}"
    jobs = gh.get(f"{gh.repo_path}/actions/runs/{run_id}/jobs", params={"filter": "latest", "per_page": 100})
    sections = []
    for job in jobs.get("jobs") or []:
        result = job.get("conclusion") or job.get("status") or "?"
        failed_steps = [s.get("name", "?") for s in job.get("steps") or [] if s.get("conclusion") == "failure"]
        title = job.get("name", "?") + (f" -- failed step(s): {', '.join(failed_steps)}" if failed_steps else "")
        if (show_all or result == "failure") and matches_step(title, step):
            sections.append(LogSection(title, result, lambda job_id=job["id"]: _github_job_log(gh, job_id)))
    return LogSource(header, sections)


def _github_job_log(gh: GitHubClient, job_id: int) -> str:
    # 404/410: a job still running or whose logs expired has none yet / any more
    r = gh.request("GET", f"{gh.repo_path}/actions/jobs/{job_id}/logs", ok=(404, 410))
    return r.text if r.status_code < 400 else ""


def render(source: LogSource, *, list_only: bool, tail: int, timestamps: bool, show_all: bool) -> str:
    title, _, url = source.header.partition("\n")
    out = [f"=== {title} ===", *([url] if url else [])]
    if not source.sections:
        out.append("(no failed steps with logs)" if not show_all else "(no steps with logs)")
        if not show_all:
            out.append("Use --all to show every step.")
    for section in source.sections:
        out.append(
            f"--- [{section.result}] {section.title} ---"
            if list_only
            else f"\n--- [{section.result}] {section.title} ---"
        )
        if not list_only:
            text = tail_lines(clean_log(section.fetch(), timestamps), tail).strip()
            out.append(text or NO_LOG)
    return "\n".join(out)


def _print(sources: list[LogSource], **render_options: bool | int) -> None:
    print("\n\n".join(render(s, **render_options) for s in sources))  # ty: ignore[invalid-argument-type]


def run_ado(
    remote: AdoRemote,
    pat: str | None,
    target_branch: str | None,
    *,
    pr_id: int | None,
    build_id: int | None,
    show_all: bool,
    step: str | None,
    **render_options: bool | int,
) -> None:
    session = ado_session(pat)
    if build_id is not None:
        r = session.get(
            f"{pr_build._base_url(remote)}/_apis/build/builds/{build_id}",
            params={"api-version": "7.1"},
            timeout=HTTP_TIMEOUT_SECS,
        )
        r.raise_for_status()
        builds = [r.json()]
    else:
        pr, source_branch = pr_build._resolve_pr_and_branch(session, remote, target_branch, None, pr_id)
        builds = pr_build.latest_per_pipeline(
            pr_build.get_builds_for_pr(session, remote, source_branch, pr["pullRequestId"])
        )
        if not builds:
            sys.exit(pr_build.no_builds_message(session, remote, pr, "show logs of"))
    sources = [ado_source(session, remote, b, show_all=show_all, step=step) for b in builds]
    _print(sources, show_all=show_all, **render_options)


def run_github(
    gh: GitHubClient,
    *,
    pr_id: int | None,
    build_id: int | None,
    show_all: bool,
    step: str | None,
    **render_options: bool | int,
) -> None:
    if build_id is not None:
        runs: list[tuple[int, str | None]] = [(build_id, None)]
    else:
        checks = gh_pr.get_pr(gh, pr_id)["statusCheckRollup"]
        buckets = {"fail", "pass", "pending", "cancel", "skipping"} if show_all else {"fail"}
        runs = list(gh_pr.check_runs(checks, buckets))
        if not runs:
            sys.exit(
                "No GitHub Actions runs "
                + ("found" if show_all else "with failed checks")
                + " for this PR."
                + ("" if show_all else " Use --all to show every run.")
            )
    sources = [github_source(gh, rid, label, show_all=show_all, step=step) for rid, label in runs]
    _print(sources, show_all=show_all, **render_options)
