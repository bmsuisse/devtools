"""`bdt dead-code`: the dead-code checks a linter can't do, behind one command and one config table.

- **sql** (`lint_sql_files`) -- `.sql` files under `sql_roots` that no Python code references;
- **routes** (`api_usage`) -- backend (FastAPI) routes that no non-generated frontend code calls and no
  `url_for(...)` references.

Configuration lives in `[tool.bdt.dead_code]` of the consuming repo's pyproject.toml. A check runs when it is
configured (`sql_roots`, resp. `[[tool.bdt.dead_code.apps]]`); `only` narrows a run to one of them, e.g. to
keep the slow one (the routes check imports the app) out of a prek hook.
"""

from __future__ import annotations

from pathlib import Path

from . import api_usage
from .api_usage import ApiUsageError
from .lint import _DEFAULT_EXCLUDE_DIR_NAMES, _iter_files, _iter_python_files
from .lint_findings import Finding
from .lint_sql_files import DEFAULT_LOADER_FUNCTIONS, check_unreferenced_sql_files

CHECKS = ("sql", "routes")


def _as_list(value: str | list[str] | None) -> list[str]:
    """A TOML list of strings; a bare string is accepted as a one-item list rather than iterated character by character."""
    return [value] if isinstance(value, str) else [str(v) for v in value or []]


def check_sql_roots(config: dict, *, repo_root: Path) -> list[Finding]:
    """`sql-file-unreferenced` over `sql_roots`. Looks at every Python file of the repo: the caller of a query
    can live anywhere."""
    exclude_dir_names = _DEFAULT_EXCLUDE_DIR_NAMES | set(
        _as_list(config.get("exclude_dirs"))
    )
    findings: list[Finding] = []
    roots: list[Path] = []
    for root in _as_list(config.get("sql_roots")):
        candidate = repo_root / root
        if candidate.is_dir() and candidate.resolve().is_relative_to(
            repo_root.resolve()
        ):
            roots.append(candidate)
        else:
            findings.append(
                Finding(
                    candidate,
                    0,
                    "lint-path-not-found",
                    f"sql_roots entry '{root}' is not a directory inside {repo_root}.",
                )
            )
    sql_files, _ = _iter_files(roots, exclude_dir_names, (".sql",))
    python_files, _ = _iter_python_files([repo_root], exclude_dir_names)
    findings.extend(
        check_unreferenced_sql_files(
            repo_root=repo_root,
            sql_files=sql_files,
            python_files=python_files,
            loader_functions=_as_list(
                config.get("sql_loader_functions", list(DEFAULT_LOADER_FUNCTIONS))
            ),
            ignore_globs=_as_list(config.get("sql_unreferenced_ignore")),
        )
    )
    return findings


def run(
    table: dict,
    *,
    repo_root: Path,
    only: list[str] | None = None,
    update_baseline: bool = False,
) -> list[Finding]:
    """Every configured check (or just those named in `only`). `update_baseline` concerns the routes check alone and
    skips the sql one."""
    unknown = [c for c in only or [] if c not in CHECKS]
    if unknown:
        raise ApiUsageError(
            f"unknown check {', '.join(unknown)} -- choose from: {', '.join(CHECKS)}"
        )
    if update_baseline and only and "routes" not in only:
        raise ApiUsageError("--update-baseline only applies to the `routes` check")
    wanted = set(only or CHECKS)
    run_sql = "sql" in wanted and not update_baseline and bool(table.get("sql_roots"))
    run_routes = "routes" in wanted and bool(table.get("apps"))
    if not run_sql and not run_routes:
        missing = " / ".join(
            part
            for part, wanted_here in (
                ("`sql_roots`", "sql" in wanted and not update_baseline),
                ("[[tool.bdt.dead_code.apps]]", "routes" in wanted),
            )
            if wanted_here
        )
        raise ApiUsageError(f"nothing to check: configure {missing} in pyproject.toml")
    findings: list[Finding] = []
    if run_sql:
        findings.extend(check_sql_roots(table, repo_root=repo_root))
    if run_routes:
        findings.extend(
            api_usage.run(table, repo_root=repo_root, update_baseline=update_baseline)
        )
    return findings
