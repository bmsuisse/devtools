"""`bdt lint`'s `sql-file-unreferenced` rule: a `.sql` file under a configured root that no Python
code references is dead weight (a query left behind after its caller was deleted or renamed).

Opt-in: only runs when `[tool.bdt.lint] sql_roots = ["backend/db/queries", ...]` is set, since which
folders hold *loadable* SQL (as opposed to schema/migration scripts that are applied, never loaded)
is a per-repo fact. A file counts as referenced when any of these holds (strongest first):

- a call `load_sql("topic", "name")` with both arguments literal, where `topic` is the file's parent
  directory name and `name` its stem (the loader function names are configurable);
- a call `load_sql("topic", <expr>)` with a non-literal name, in a Python file that also contains the
  stem as a string literal (covers `name = "a" if x else "b"` / lookup tables / f-string parts);
- the same with a non-literal topic (any file calling the loader dynamically may name any stem);
- a string literal that is a path ending in the file's repo-relative path
  (`get_sql_with_prm_list("backend/api/sql/x.sql")`);
- a string literal equal to the bare filename in a Python file under the SQL folder's parent
  directory (`Path(__file__).parent.parent / "sql" / "x.sql"`).

Anything else is reported. Heuristic by design -- it never executes code, so a SQL file reached
through a fully computed path is a false positive; list such files in `sql_unreferenced_ignore`.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterable
from fnmatch import fnmatch
from pathlib import Path

from .lint_findings import Finding

RULE = "sql-file-unreferenced"
DEFAULT_LOADER_FUNCTIONS = ("load_sql",)


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _References:
    def __init__(self, repo_root: Path, loader_functions: frozenset[str]) -> None:
        self.repo_root = repo_root
        self.loader_functions = loader_functions
        self.exact: set[tuple[str, str]] = set()  # (topic, name) from fully literal loader calls
        self.dynamic_by_topic: dict[str, set[str]] = {}  # topic -> string literals of files with a dynamic-name call
        self.dynamic_any_topic: set[str] = set()  # string literals of files with a dynamic-topic call
        self.sql_path_literals: set[str] = set()  # every string literal containing ".sql"
        self.bare_by_file: list[tuple[Path, set[str]]] = []  # (python file, string literals ending in .sql)

    def scan(self, python_files: Iterable[Path]) -> None:
        for path in python_files:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError, UnicodeDecodeError, OSError, ValueError:
                continue
            literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            sql_literals = {s for s in literals if s.endswith(".sql")}
            self.sql_path_literals |= sql_literals
            if sql_literals:
                self.bare_by_file.append((path.resolve(), sql_literals))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and _call_name(node) in self.loader_functions and node.args):
                    continue
                topic = _literal(node.args[0])
                name = _literal(node.args[1]) if len(node.args) > 1 else None
                if topic is not None and name is not None:
                    self.exact.add((topic, name))
                elif topic is not None:
                    self.dynamic_by_topic.setdefault(topic, set()).update(literals)
                else:
                    self.dynamic_any_topic |= literals

    def is_referenced(self, sql_file: Path) -> bool:
        stem, topic = sql_file.stem, sql_file.parent.name
        if (topic, stem) in self.exact:
            return True
        if stem in self.dynamic_by_topic.get(topic, ()) or stem in self.dynamic_any_topic:
            return True
        rel = sql_file.resolve().relative_to(self.repo_root.resolve()).as_posix()
        if any("/" in lit and rel.endswith(lit.lstrip("./")) for lit in self.sql_path_literals):
            return True
        scope = sql_file.resolve().parent.parent
        return any(sql_file.name in lits and py.is_relative_to(scope) for py, lits in self.bare_by_file)


def _iter_sql_files(roots: Iterable[Path], exclude_dir_names: frozenset[str]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in exclude_dir_names]
            found.extend(Path(dirpath, f) for f in filenames if f.endswith(".sql"))
    return sorted(found)


def check_unreferenced_sql_files(
    *,
    repo_root: Path,
    sql_roots: Iterable[str],
    python_files: Iterable[Path],
    exclude_dir_names: frozenset[str],
    loader_functions: Iterable[str] = DEFAULT_LOADER_FUNCTIONS,
    ignore_globs: Iterable[str] = (),
) -> list[Finding]:
    """One `sql-file-unreferenced` finding per `.sql` file under `sql_roots` (repo-relative) that no
    Python file in `python_files` references. A configured root that doesn't exist is itself a finding
    (a typo would otherwise silently disable the check)."""
    findings: list[Finding] = []
    roots: list[Path] = []
    for root in sql_roots:
        candidate = repo_root / root
        if candidate.is_dir():
            roots.append(candidate)
        else:
            findings.append(
                Finding(
                    candidate,
                    0,
                    "lint-path-not-found",
                    f"sql_roots entry '{root}' is not a directory under {repo_root}.",
                )
            )
    sql_files = _iter_sql_files(roots, exclude_dir_names)
    if not sql_files:
        return findings

    refs = _References(repo_root, frozenset(loader_functions))
    refs.scan(python_files)
    ignores = list(ignore_globs)
    for sql_file in sql_files:
        rel = sql_file.resolve().relative_to(repo_root.resolve()).as_posix()
        if any(fnmatch(rel, g) for g in ignores) or refs.is_referenced(sql_file):
            continue
        findings.append(
            Finding(
                sql_file,
                0,
                RULE,
                "no Python code references this SQL file (load_sql topic/name or path) -- delete it, or list it in "
                "[tool.bdt.lint] sql_unreferenced_ignore if it is reached some other way.",
            )
        )
    return findings
