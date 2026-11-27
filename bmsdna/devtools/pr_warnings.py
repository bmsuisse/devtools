"""`bdt pr warnings`: list the warnings of a PR's builds (Azure DevOps) / workflow runs (GitHub), the ones
`bdt pr status` only counts."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

import requests

from bmsdna.devtools import build_warnings, gh_pr, pr_build, pr_logs
from bmsdna.devtools.ado_auth import ado_session
from bmsdna.devtools.build_warnings import BuildWarning
from bmsdna.devtools.cli_tools import HTTP_TIMEOUT_SECS
from bmsdna.devtools.github_api import GitHubClient
from bmsdna.devtools.gitrepo import AdoRemote


@dataclass
class WarningSource:
    """A build / workflow run and its warnings; `count` is what it reports, which can exceed what could be listed."""

    id: int
    name: str
    result: str
    url: str
    count: int
    warnings: list[BuildWarning]


def ado_source(session: requests.Session, remote: AdoRemote, build: dict) -> WarningSource:
    def fetch_log(url: str) -> str:
        r = session.get(url, params={"api-version": "7.1"}, timeout=HTTP_TIMEOUT_SECS)
        r.raise_for_status()
        return r.text

    try:
        records = pr_build.get_timeline_records(session, remote, build["id"])
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            raise
        records = []  # queued / postponed: no timeline yet
    by_id = {rec.get("id"): rec for rec in records}
    found = build_warnings.ado_warnings(records, lambda rec: pr_logs.record_path(by_id, rec), fetch_log)
    return WarningSource(
        build["id"],
        (build.get("definition") or {}).get("name", "?"),
        build.get("result") or build.get("status") or "?",
        pr_build.build_web_url(remote, build["id"]),
        max(build_warnings.ado_warning_count(records), len(found)),
        found,
    )


def github_source(gh: GitHubClient, run_id: int, label: str | None) -> WarningSource:
    run = gh.get(f"{gh.repo_path}/actions/runs/{run_id}")
    found = build_warnings.github_warnings(gh, run_id)
    return WarningSource(
        run_id,
        label or run.get("name") or "?",
        run.get("conclusion") or run.get("status") or "?",
        run.get("html_url", ""),
        len(found),
        found,
    )


def render(source: WarningSource) -> str:
    kind = "Run" if "/actions/" in source.url else "Build"
    out = [f"=== {kind} {source.id}  {source.name}  [{source.result}] ==="]
    if source.url:
        out.append(source.url)
    if not source.count:
        return "\n".join([*out, "No warnings."])
    out.append(f"{source.count} Warning{'s' * (source.count != 1)}")
    if len(source.warnings) < source.count:
        out.append(f"(only {len(source.warnings)} of them could be listed; see the logs: bdt pr logs {source.id})")
    steps: dict[str, list[str]] = {}
    for w in source.warnings:
        steps.setdefault(w.step, []).append(w.message)
    for step, messages in steps.items():
        out.append(f"\n--- {step} ---")
        out.extend(messages)
    return "\n".join(out)


def to_json(sources: list[WarningSource]) -> str:
    return json.dumps(
        [
            {
                "id": s.id,
                "name": s.name,
                "result": s.result,
                "url": s.url,
                "count": s.count,
                "warnings": [{"step": w.step, "message": w.message} for w in s.warnings],
            }
            for s in sources
        ],
        indent=2,
        ensure_ascii=False,
    )


def _print(sources: list[WarningSource], as_json: bool) -> None:
    print(to_json(sources) if as_json else "\n\n".join(render(s) for s in sources))


def run_ado(
    remote: AdoRemote,
    pat: str | None,
    target_branch: str | None,
    *,
    pr_id: int | None,
    build_id: int | None,
    as_json: bool,
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
            sys.exit(pr_build.no_builds_message(session, remote, pr, "show warnings of"))
    _print([ado_source(session, remote, b) for b in builds], as_json)


def run_github(gh: GitHubClient, *, pr_id: int | None, build_id: int | None, as_json: bool) -> None:
    if build_id is not None:
        runs: list[tuple[int, str | None]] = [(build_id, None)]
    else:
        checks = gh_pr.get_pr(gh, pr_id)["statusCheckRollup"]
        runs = list(gh_pr.check_runs(checks, pr_logs.ALL_BUCKETS))
        if not runs:
            sys.exit("No GitHub Actions runs found for this PR.")
    _print([github_source(gh, rid, label) for rid, label in runs], as_json)
