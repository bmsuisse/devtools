"""`bdt lint` orchestration (bmsuisse/skills#52): discovers which Python files to
scan (a directory walk by default, or an explicit file list -- see `_iter_python_files`
so this also works as a prek/pre-commit hook scanning only the staged diff), runs
the SQL rule engine (lint_sql) and the pydantic-model-placement check (lint_models)
over each, the TypeScript hand-wired-HTTP check (lint_typescript, bmsuisse/devtools#52)
over each TypeScript file, and -- unless bypassed -- the tooling-config check
(lint_tooling) once for the whole run. The opt-in unreferenced-`.sql`-file check (lint_sql_files,
`[tool.bdt.lint] sql_roots`) also runs once per run, over the whole repo regardless of `paths`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .bdt_config import find_pyproject, load_bdt_table
from .lint_findings import Finding, render_findings
from .lint_models import (
    DEFAULT_ALLOWED_SUBDIRS,
    DEFAULT_API_DIR_NAMES,
    DEFAULT_BASE_CLASS_NAMES,
    DEFAULT_FIELD_THRESHOLD,
    check_models_file,
)
from .lint_sql import check_sql_file, require_sqlglot
from .lint_sql_files import DEFAULT_LOADER_FUNCTIONS, check_unreferenced_sql_files
from .lint_tooling import check_tooling
from .lint_typescript import (
    DEFAULT_NON_JSON_MARKERS,
    DEFAULT_TS_EXCLUDE_DIR_NAMES,
    TS_SUFFIXES,
    check_typescript_file,
    find_generator,
    is_excluded_ts_file,
)

_DEFAULT_EXCLUDE_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".worktrees",
        ".claude",
        "dist",
        "build",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".eggs",
    }
)


@dataclass(frozen=True, slots=True)
class LintResult:
    findings: list[Finding]
    tooling_skipped: bool

    @property
    def ok(self) -> bool:
        return not self.findings


def _iter_python_files(paths: list[Path], exclude_dir_names: frozenset[str]) -> tuple[list[Path], list[Path]]:
    return _iter_files(paths, exclude_dir_names, (".py",))


def _iter_files(paths: list[Path], exclude_dir_names: frozenset[str], suffixes: tuple[str, ...]) -> tuple[list[Path], list[Path]]:
    """`paths` with directories expanded to every non-excluded file ending in one of `suffixes` under them
    (sorted, for stable output); a path that's already such a file is used as-is
    regardless of exclude_dir_names -- an explicit file (e.g. from a pre-commit hook's
    staged-file list) is always scanned, even if it happens to sit under a normally-excluded
    directory name. Deduplicated so the same file is never scanned twice.

    Also returns every entry of `paths` that doesn't exist at all, so a typo'd or stale
    filename (e.g. in a pre-commit hook's staged-file list) surfaces as a finding instead of
    silently scanning nothing and reporting a clean pass. A path that exists but isn't a
    directory or a `.py` file (e.g. a non-Python file a hook happened to pass through) is
    intentionally *not* reported here -- skipping it is correct, not an error.
    """
    files: list[Path] = []
    missing: list[Path] = []
    seen: set[Path] = set()

    def add(candidate: Path) -> None:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            files.append(candidate)

    for path in paths:
        if path.is_dir():
            found: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d not in exclude_dir_names]
                found.extend(Path(dirpath, f) for f in filenames if f.endswith(suffixes))
            for candidate in sorted(found):
                add(candidate)
        elif path.is_file():
            if path.suffix in suffixes:
                add(path)
        else:
            missing.append(path)

    return files, missing


def _as_list(value: str | list[str] | None) -> list[str]:
    """A TOML list of strings; a bare string is accepted as a one-item list rather than iterated character by character."""
    return [value] if isinstance(value, str) else [str(v) for v in value or []]


def _check_sql_roots(config: dict, *, repo_root: Path, exclude_dir_names: frozenset[str], python_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    roots: list[Path] = []
    for root in _as_list(config.get("sql_roots")):
        candidate = repo_root / root
        if candidate.is_dir() and candidate.resolve().is_relative_to(repo_root.resolve()):
            roots.append(candidate)
        else:
            findings.append(Finding(candidate, 0, "lint-path-not-found", f"sql_roots entry '{root}' is not a directory inside {repo_root}."))
    sql_files, _ = _iter_files(roots, exclude_dir_names, (".sql",))
    findings.extend(
        check_unreferenced_sql_files(
            repo_root=repo_root,
            sql_files=sql_files,
            python_files=python_files,
            loader_functions=_as_list(config.get("sql_loader_functions", list(DEFAULT_LOADER_FUNCTIONS))),
            ignore_globs=_as_list(config.get("sql_unreferenced_ignore")),
        )
    )
    return findings


def run(paths: list[str], *, root: Path | None = None, skip_tooling_check: bool = False) -> LintResult:
    """Runs every `bdt lint` check.

    `paths` -- files and/or directories to scan; empty means "scan `root`, recursively" (the opt-in
    `sql-file-unreferenced` rule always looks at the whole repo, whatever `paths` says).
    `root` -- where to look for pyproject.toml / prek.toml (defaults to cwd) and, with no
    `paths`, what to scan; also the base a relative `paths` entry and the pydantic-model
    check's api/-tree detection are resolved against.
    `skip_tooling_check` -- bypass the ty/ruff/pytest/prek config check for this run (the
    `--no-tooling-check` CLI flag); `[tool.bdt.lint] skip_tooling_check = true` in the
    scanned repo's pyproject.toml bypasses it permanently -- either way, this check never
    hard-blocks silently, the caller always sees why it didn't run.
    """
    root = root or Path.cwd()
    target_paths = [Path(p) for p in paths] if paths else [root]
    config = load_bdt_table("lint", root)

    exclude_dir_names = _DEFAULT_EXCLUDE_DIR_NAMES | set(config.get("exclude_dirs", []) or [])
    field_threshold = int(config.get("pydantic_field_threshold", DEFAULT_FIELD_THRESHOLD))
    base_class_names = frozenset(config.get("pydantic_base_classes", list(DEFAULT_BASE_CLASS_NAMES)))
    allowed_subdirs = frozenset(config.get("pydantic_allowed_subdirs", list(DEFAULT_ALLOWED_SUBDIRS)))
    api_dir_names = frozenset(config.get("pydantic_api_dir_names", list(DEFAULT_API_DIR_NAMES)))

    pyproject_path = find_pyproject(root)
    repo_root = pyproject_path.parent if pyproject_path else root

    ts_exclude_globs = [str(g) for g in config.get("ts_exclude_globs", []) or []]
    ts_markers = DEFAULT_NON_JSON_MARKERS + tuple(str(m) for m in config.get("ts_non_json_markers", []) or [])

    python_files, missing_paths = _iter_python_files(target_paths, exclude_dir_names)
    ts_files, _ = _iter_files(target_paths, exclude_dir_names | DEFAULT_TS_EXCLUDE_DIR_NAMES, TS_SUFFIXES)
    if python_files:
        require_sqlglot()

    findings: list[Finding] = [
        Finding(path, 0, "lint-path-not-found", f"'{path}' doesn't exist -- nothing was scanned for it.") for path in missing_paths
    ]
    for path in python_files:
        findings.extend(check_sql_file(path))
        findings.extend(
            check_models_file(
                path,
                repo_root=repo_root,
                field_threshold=field_threshold,
                base_class_names=base_class_names,
                allowed_subdirs=allowed_subdirs,
                api_dir_names=api_dir_names,
            )
        )

    generator_cache: dict[Path, str | None] = {}
    for ts_path in ts_files:
        if is_excluded_ts_file(ts_path, repo_root=repo_root, exclude_globs=ts_exclude_globs):
            continue
        generator = find_generator(ts_path, repo_root=repo_root, cache=generator_cache)
        if generator is None:
            continue  # no generated API client in this package -- nothing to use instead of hand-wiring
        findings.extend(check_typescript_file(ts_path, generator=generator, non_json_markers=ts_markers))

    if config.get("sql_roots"):
        # References can live anywhere in the repo, so this looks past `paths` (a prek hook's staged-file list).
        whole_repo = len(target_paths) == 1 and target_paths[0].resolve() == repo_root.resolve()
        findings.extend(
            _check_sql_roots(
                config,
                repo_root=repo_root,
                exclude_dir_names=exclude_dir_names,
                python_files=python_files if whole_repo else _iter_python_files([repo_root], exclude_dir_names)[0],
            )
        )

    tooling_skipped = skip_tooling_check or bool(config.get("skip_tooling_check", False))
    if not tooling_skipped:
        findings.extend(check_tooling(root))

    return LintResult(findings=findings, tooling_skipped=tooling_skipped)


def print_report(result: LintResult) -> int:
    """Prints `result` and returns the process exit code (0 clean, 1 findings)."""
    if result.ok:
        suffix = " (tooling check skipped)" if result.tooling_skipped else ""
        print(f"bdt lint: no issues found{suffix}")
        return 0
    print(render_findings(result.findings))
    print(f"\n{len(result.findings)} issue(s) found.")
    return 1
