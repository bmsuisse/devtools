"""Fetch recent Application Insights / Log Analytics logs via `az monitor app-insights query`.

Unlike the other commands, this one takes no repo-specific defaults: pass
--resource-group/--app-insights explicitly (or set AZURE_RESOURCE_GROUP /
AZURE_APP_INSIGHTS), since which Azure resource "this repo" maps to isn't
derivable from the git remote the way Azure DevOps org/project/repo is.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

SEVERITY_MAP = {"verbose": 0, "information": 1, "warning": 2, "error": 3, "critical": 4}

COLORS = {
    "CRITICAL": "\033[95m",
    "ERROR   ": "\033[91m",
    "WARNING ": "\033[93m",
    "INFO    ": "\033[97m",
    "VERBOSE ": "\033[90m",
    "RESET": "\033[0m",
}


def run_az(*args: str) -> tuple[int, str]:
    result = subprocess.run(["az", *args], capture_output=True, text=True, shell=sys.platform == "win32")
    return result.returncode, result.stdout + result.stderr


def find_app_insights(resource_group: str) -> str | None:
    code, out = run_az(
        "resource", "list",
        "--resource-group", resource_group,
        "--resource-type", "Microsoft.Insights/components",
        "--query", "[0].name",
        "--output", "tsv",
    )
    name = out.strip()
    return name if code == 0 and name else None


def query_roles(app_insights: str, resource_group: str, minutes: int) -> tuple[dict[str, int] | None, str | None]:
    kql = f"""
union traces, exceptions
| where timestamp > ago({minutes}m)
| summarize count() by cloud_RoleName
| order by count_ desc
""".strip()
    code, out = run_az(
        "monitor", "app-insights", "query",
        "--app", app_insights,
        "--resource-group", resource_group,
        "--analytics-query", kql,
        "--output", "json",
    )
    if code != 0:
        return None, out
    try:
        table = json.loads(out)["tables"][0]
    except json.JSONDecodeError:
        table = json.loads(out.split("\n")[0])["tables"][0]
    col_names = [c["name"] for c in table["columns"]]
    role_col = next((i for i, n in enumerate(col_names) if n == "cloud_RoleName"), None)
    count_col = next((i for i, n in enumerate(col_names) if n == "count_"), None)
    if role_col is None:
        return None, f"cloud_RoleName column not found; got: {col_names}"
    counts: dict[str, int] = {}
    for row in table["rows"]:
        role = row[role_col] or "(unknown)"
        counts[role] = int(row[count_col]) if count_col is not None and row[count_col] else 1
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True)), None


def query_logs(
    app_insights: str, resource_group: str, minutes: int, min_severity: int, role: str
) -> tuple[list | None, str | None]:
    role_filter = "" if role == "all" else f'| where cloud_RoleName == "{role}"'
    kql = f"""
union traces, exceptions
| where timestamp > ago({minutes}m)
| where severityLevel >= {min_severity}
{role_filter}
| order by timestamp asc
| take 500
""".strip()
    code, out = run_az(
        "monitor", "app-insights", "query",
        "--app", app_insights,
        "--resource-group", resource_group,
        "--analytics-query", kql,
        "--output", "json",
    )
    if code != 0:
        return None, out
    try:
        table = json.loads(out)["tables"][0]
    except json.JSONDecodeError:
        table = json.JSONDecoder().raw_decode(out)[0]["tables"][0]
    col_index = {col["name"]: i for i, col in enumerate(table["columns"])}

    def col(row: list, *names: str) -> str:
        for name in names:
            if name in col_index:
                return row[col_index[name]] or ""
        return ""

    results = []
    for row in table["rows"]:
        sev: int | None = row[col_index["severityLevel"]] if "severityLevel" in col_index else None
        lvl = (
            {0: "VERBOSE ", 1: "INFO    ", 2: "WARNING ", 3: "ERROR   ", 4: "CRITICAL"}.get(sev, "UNKNOWN ")
            if sev is not None else "UNKNOWN "
        )
        msg = col(row, "message", "outerMessage", "innermostMessage")
        item_type = col(row, "itemType")
        exc = f" | {col(row, 'type')}: {col(row, 'outerMessage')}" if item_type == "exception" else ""
        results.append({"timestamp": row[col_index["timestamp"]], "lvl": lvl, "msg": msg, "exc": exc})
    return results, None


def print_roles(app_insights: str, resource_group: str, minutes: int) -> None:
    print(f"Querying roles for last {minutes} min from {app_insights}...", flush=True)
    roles, err = query_roles(app_insights, resource_group, minutes)
    if roles is None:
        found = find_app_insights(resource_group)
        if not found:
            sys.exit(f"ERROR: No App Insights in {resource_group}.")
        roles, err = query_roles(found, resource_group, minutes)
    if roles is None:
        sys.exit(f"ERROR: {err}")
    print(f"\n{'Role':<50} {'Messages':>10}\n" + "-" * 62)
    for role, count in roles.items():
        print(f"{role:<50} {count:>10}")
    print("\nPass --role <name> to fetch logs for a specific role.")


def print_logs(
    app_insights: str, resource_group: str, minutes: int, level: str, role: str, no_color: bool
) -> None:
    min_severity = SEVERITY_MAP[level]
    print(f"Querying last {minutes} min | role={role} | severity>={level}...", flush=True)
    rows, err = query_logs(app_insights, resource_group, minutes, min_severity, role)
    if rows is None:
        found = find_app_insights(resource_group)
        if not found:
            sys.exit(f"ERROR: No App Insights in {resource_group}.")
        rows, err = query_logs(found, resource_group, minutes, min_severity, role)
    if rows is None:
        sys.exit(f"ERROR:\n{err}")
    if not rows:
        print(f"No entries in the last {minutes} minutes.")
        return

    print(f"--- {len(rows)} entries ---")
    for row in rows:
        ts = datetime.fromisoformat(row["timestamp"].rstrip("Z")).replace(tzinfo=timezone.utc).strftime("%H:%M:%S")
        line = f"{ts} [{row['lvl']}] {row['msg']}{row['exc']}"
        if no_color:
            print(line)
        else:
            color = COLORS.get(row["lvl"], "")
            print(f"{color}{line}{COLORS['RESET']}")
