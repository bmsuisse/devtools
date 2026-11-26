"""`bdt pr logs`: print the logs of a PR's builds (Azure DevOps) / workflow runs (GitHub).

`bdt pr status` only shows the failed steps while it reports status; this is the dedicated viewer:
every step is listed, but only failed ones (or those picked with `--step`, or all with `--all`) print their
log; succeeded ones stay one line. Also as JSON.
"""

from __future__ import annotations

import json
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


ALL_BUCKETS = {"fail", "pass", "pending", "cancel", "skipping", "waiting_approval"}
FAILED_RESULTS = frozenset({"failed", "failure", "timed_out", "action_required"})


@dataclass
class LogSection:
    """One step (Azure DevOps) or job (GitHub) with a log. `fetch` is only called when the text is shown."""

    title: str
    result: str
    fetch: Callable[[], str] = field(repr=False, default=lambda: "")

    @property
    def failed(self) -> bool:
        return self.result.casefold() in FAILED_RESULTS


@dataclass
class LogSource:
    """A build / workflow run and its sections."""

    id: int
    name: str
    result: str
    url: str
    sections: list[LogSection]

    @property
    def header(self) -> str:
        kind = "Run" if "/actions/" in self.url else "Build"
        return f"{kind} {self.id}  {self.name}  [{self.result}]"


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


def ado_sections(records: list[dict], fetch_log: Callable[[str], str], *, step: str | None) -> list[LogSection]:
    """The timeline's tasks that have a log, as `Stage / Job / Task` sections in start order;
    `step` keeps those whose path contains it (case-insensitive).
    """
    by_id = {rec.get("id"): rec for rec in records}

    def ancestors(rec: dict) -> list[dict]:
        found = []
        parent = by_id.get(rec.get("parentId"))
        while parent is not None:
            found.append(parent)
            parent = by_id.get(parent.get("parentId"))
        return found

    def path(rec: dict) -> str:
        names = [a.get("name") or "?" for a in ancestors(rec) if a.get("type") in ("Stage", "Job")]
        return " / ".join([*reversed(names), rec.get("name") or "?"])

    def position(rec: dict) -> tuple[str, str, int]:
        # a skipped task never started: place it by its job and its `order` in it
        job = next((a for a in ancestors(rec) if a.get("type") == "Job"), {})
        return job.get("startTime") or "", job.get("id") or "", rec.get("order") or 0

    sections = []
    for rec in sorted((r for r in records if r.get("type") == "Task"), key=position):
        title = path(rec)
        if matches_step(title, step):
            url = (rec.get("log") or {}).get("url")
            sections.append(
                LogSection(
                    title,
                    rec.get("result") or rec.get("state") or "?",
                    (lambda url=url: fetch_log(url)) if url else str,
                )
            )
    return sections


def ado_source(session: requests.Session, remote: AdoRemote, build: dict, *, step: str | None) -> LogSource:
    def fetch_log(url: str) -> str:
        r = session.get(url, params={"api-version": "7.1"}, timeout=HTTP_TIMEOUT_SECS)
        r.raise_for_status()
        return r.text

    pipeline = (build.get("definition") or {}).get("name", "?")
    state = build.get("result") or build.get("status") or "?"
    try:
        records = pr_build.get_timeline_records(session, remote, build["id"])
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            raise
        records = []  # queued / postponed: no timeline yet
    return LogSource(
        build["id"],
        pipeline,
        state,
        pr_build.build_web_url(remote, build["id"]),
        ado_sections(records, fetch_log, step=step),
    )


def github_source(gh: GitHubClient, run_id: int, label: str | None, *, step: str | None) -> LogSource:
    """A workflow run's jobs. GitHub serves one log per *job* (not per step), so a failed job's
    header names its failed steps instead."""
    run = gh.get(f"{gh.repo_path}/actions/runs/{run_id}")
    name = label or run.get("name") or "?"
    state = run.get("conclusion") or run.get("status") or "?"
    jobs = gh.paginate(f"{gh.repo_path}/actions/runs/{run_id}/jobs", {"filter": "latest"}, items_key="jobs")
    sections = []
    for job in jobs:
        result = job.get("conclusion") or job.get("status") or "?"
        failed_steps = [s.get("name", "?") for s in job.get("steps") or [] if s.get("conclusion") == "failure"]
        title = job.get("name", "?") + (f" -- failed step(s): {', '.join(failed_steps)}" if failed_steps else "")
        if matches_step(title, step):
            sections.append(LogSection(title, result, lambda job_id=job["id"]: _github_job_log(gh, job_id)))
    return LogSource(run_id, name, state, run.get("html_url", ""), sections)


def _github_job_log(gh: GitHubClient, job_id: int) -> str:
    # 404/410: a job still running or whose logs expired has none yet / any more
    r = gh.request("GET", f"{gh.repo_path}/actions/jobs/{job_id}/logs", ok=(404, 410))
    return r.text if r.status_code < 400 else ""


@dataclass(frozen=True)
class Options:
    """What to print: the `bdt pr logs` flags."""

    show_all: bool = False
    step: str | None = None
    tail: int = 0
    list_only: bool = False
    timestamps: bool = False
    as_json: bool = False


def shows_log(section: LogSection, opts: Options) -> bool:
    """Failed steps always print their log; the others only with `--all` or when `--step` picked them."""
    return not opts.list_only and (section.failed or opts.show_all or opts.step is not None)


def step_text(section: LogSection, opts: Options) -> str:
    return tail_lines(clean_log(section.fetch(), opts.timestamps), opts.tail).strip() or NO_LOG


def render(source: LogSource, opts: Options) -> str:
    out = [f"=== {source.header} ===", source.url] if source.url else [f"=== {source.header} ==="]
    if not source.sections:
        out.append("(no steps with logs)")
    for section in source.sections:
        if not shows_log(section, opts):
            out.append(f"[{section.result}] {section.title}")
            continue
        out.append(f"\n--- [{section.result}] {section.title} ---")
        out.append(step_text(section, opts))
    return "\n".join(out)


def to_json(sources: list[LogSource], opts: Options) -> str:
    """One JSON document: the sources, each with its steps; `log` is the text for steps that print one
    (see `shows_log`), else null."""
    return json.dumps(
        [
            {
                "id": src.id,
                "name": src.name,
                "result": src.result,
                "url": src.url,
                "steps": [
                    {
                        "name": sec.title,
                        "result": sec.result,
                        "log": step_text(sec, opts) if shows_log(sec, opts) else None,
                    }
                    for sec in src.sections
                ],
            }
            for src in sources
        ],
        indent=2,
        ensure_ascii=False,
    )


def _print(sources: list[LogSource], opts: Options) -> None:
    if opts.as_json:
        print(to_json(sources, opts))
    else:
        print("\n\n".join(render(src, opts) for src in sources))


def run_ado(
    remote: AdoRemote,
    pat: str | None,
    target_branch: str | None,
    *,
    pr_id: int | None,
    build_id: int | None,
    opts: Options,
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
    sources = [ado_source(session, remote, b, step=opts.step) for b in builds]
    _print(sources, opts)


def run_github(
    gh: GitHubClient,
    *,
    pr_id: int | None,
    build_id: int | None,
    opts: Options,
) -> None:
    if build_id is not None:
        runs: list[tuple[int, str | None]] = [(build_id, None)]
    else:
        checks = gh_pr.get_pr(gh, pr_id)["statusCheckRollup"]
        runs = list(gh_pr.check_runs(checks, ALL_BUCKETS))
        if not runs:
            sys.exit("No GitHub Actions runs found for this PR.")
    sources = [github_source(gh, rid, label, step=opts.step) for rid, label in runs]
    _print(sources, opts)
