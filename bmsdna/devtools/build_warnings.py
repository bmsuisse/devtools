"""The warnings of a build (Azure DevOps timeline issues) / workflow run (GitHub check-run annotations):
counted for `bdt pr status`, listed by `bdt pr warnings`. Tools that only print their warnings are scanned by
`tool_warnings`, which `bdt ci warnings` uses to report them to the CI system in the first place."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import requests

from bmsdna.devtools.github_api import GitHubClient

LOG_WARNING_RE = re.compile(r"##\[warning\]\s*(.*)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# rolldown (the bundler of vite 8) prints its warnings as `<yellow>[CODE] <reset>message`
BUNDLER_WARNING_RE = re.compile(r"\x1b\[33m\[([A-Z][A-Z0-9_]+)\]\s*(?:\x1b\[0m)?(.+)")
PYTEST_WARNING_RE = re.compile(r"^\s*(\S+:\d+): (\w*Warning): (.*)$")


@dataclass(frozen=True)
class BuildWarning:
    step: str
    message: str


def count_text(count: int, build_id: int) -> str:
    return f"{count} Warning{'s' * (count != 1)} (see with bdt pr warnings --build-id {build_id})"


def _is_task(rec: dict) -> bool:
    return rec.get("type") == "Task"


def ado_warning_issues(rec: dict) -> list[dict]:
    return [i for i in rec.get("issues") or [] if (i.get("type") or "").casefold() == "warning"]


def _vite_warning(lines: list[str], start: int) -> str:
    """`(!) message` plus the bullet lines under it (`- hint`)."""
    parts = [lines[start].removeprefix("(!) ").strip()]
    for line in lines[start + 1 :]:
        if not line.lstrip().startswith("- "):
            break
        parts.append(line.strip().removeprefix("- "))
    return " ".join(parts)


def _pytest_warnings(lines: list[str], start: int) -> list[str]:
    """The entries of a pytest `warnings summary` section, which ends at `-- Docs:`."""
    found = []
    for line in lines[start + 1 :]:
        if line.startswith("-- Docs:"):
            break
        if m := PYTEST_WARNING_RE.match(line):
            found.append(f"{m.group(1)}: {m.group(2)}: {m.group(3)}")
    return found


def tool_warnings(output: str) -> list[str]:
    """What tools print for warnings, without telling the CI system: rolldown/vite (`[CODE] message`, `(!) message`)
    and the entries of pytest's `warnings summary`, each once, in order."""
    found: list[str] = []
    raw_lines = output.splitlines()
    lines = [ANSI_RE.sub("", line) for line in raw_lines]
    for i, line in enumerate(lines):
        if m := BUNDLER_WARNING_RE.match(raw_lines[i]):
            found.append(f"[{m.group(1)}] {ANSI_RE.sub('', m.group(2)).strip()}")
        elif line.startswith("(!) "):
            found.append(_vite_warning(lines, i))
        elif line.strip("= ") == "warnings summary":
            found.extend(_pytest_warnings(lines, i))
    return list(dict.fromkeys(found))


def ado_warning_count(records: list[dict]) -> int:
    """Warnings in a build's timeline. A task's `warningCount` is the real number; its `issues` list is capped
    by Azure DevOps. Stage/job records only count their own issues, as their `warningCount` sums their tasks'."""
    total = 0
    for rec in records:
        listed = len(ado_warning_issues(rec))
        total += max(listed, rec.get("warningCount") or 0) if _is_task(rec) else listed
    return total


def issue_text(issue: dict) -> str:
    message = (issue.get("message") or "").strip()
    data = issue.get("data") or {}
    source = data.get("sourcepath")
    if not source:
        return message
    line = data.get("linenumber")
    return f"{source}:{line}: {message}" if line else f"{source}: {message}"


def log_warnings(log: str) -> list[str]:
    return [m.group(1).strip() for line in log.splitlines() if (m := LOG_WARNING_RE.search(line))]


def ado_warnings(
    records: list[dict], path: Callable[[dict], str], fetch_log: Callable[[str], str]
) -> list[BuildWarning]:
    """Every warning of the timeline, by step. Where a task has more warnings than its (capped) `issues`
    list holds, they are read from its log instead."""
    found = []
    for rec in records:
        messages = [issue_text(i) for i in ado_warning_issues(rec)]
        url = (rec.get("log") or {}).get("url")
        if _is_task(rec) and (rec.get("warningCount") or 0) > len(messages) and url:
            try:
                from_log = log_warnings(fetch_log(url))
            except requests.RequestException:
                from_log = []
            if len(from_log) > len(messages):
                messages = from_log
        found += [BuildWarning(path(rec), m) for m in messages]
    return found


def annotation_text(annotation: dict) -> str:
    where = annotation.get("path") or ""
    if where and annotation.get("start_line"):
        where += f":{annotation['start_line']}"
    parts = [where, (annotation.get("title") or "").strip(), (annotation.get("message") or "").strip()]
    return ": ".join(p for p in parts if p)


def github_warnings(gh: GitHubClient, run_id: int) -> list[BuildWarning]:
    """The `warning` annotations of the run's jobs (GitHub shows them on the run's summary page)."""
    jobs = gh.paginate(f"{gh.repo_path}/actions/runs/{run_id}/jobs", {"filter": "latest"}, items_key="jobs")
    found = []
    for job in jobs:
        if job.get("conclusion") == "skipped":
            continue
        annotations = gh.paginate(f"{gh.repo_path}/check-runs/{job['id']}/annotations")
        found.extend(
            BuildWarning(job.get("name", "?"), annotation_text(a))
            for a in annotations
            if a.get("annotation_level") == "warning"
        )
    return found
