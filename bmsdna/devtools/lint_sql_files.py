"""The `sql` check of `bdt dead-code` (rule `sql-file-unreferenced`): a `.sql` file under a configured root that no Python
code references is dead weight (a query left behind after its caller was deleted or renamed).

Part of `bdt dead-code`; opt-in: only runs when `[tool.bdt.dead_code] sql_roots = ["backend/db/queries", ...]` is set, since which
folders hold *loadable* SQL (as opposed to schema/migration scripts that are applied, never loaded)
is a per-repo fact. A file counts as referenced when any of these holds (strongest first):

- a call `load_sql("topic", "name")` (positional or `topic=`/`name=` keywords) with both arguments literal,
  where `topic` is the file's parent directory name and `name` its stem (loader function names are configurable);
- a call `load_sql("topic", <expr>)` with a non-literal name, in a Python file that also contains the
  stem as a string literal (covers `name = "a" if x else "b"` / lookup tables / f-string parts);
- the same with a non-literal topic (any file calling the loader dynamically may name any stem);
- a string literal that is a path whose trailing segments equal the file's repo-relative path
  (`get_sql_with_prm_list("backend/api/sql/x.sql")`);
- the bare filename as a literal in a Python file under the SQL folder's parent directory
  (`Path(__file__).parent.parent / "sql" / "x.sql"`).

Anything else is reported. Heuristic by design -- it never executes code, so a SQL file reached
through a fully computed path is a false positive; list such files in `sql_unreferenced_ignore`.
A Python file that cannot be parsed is reported too (its references are unknown, so the result is not trustworthy).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from fnmatch import fnmatch
from pathlib import Path

from .lint_findings import Finding

RULE = "sql-file-unreferenced"
RULE_UNPARSEABLE = "sql-check-python-unparseable"
DEFAULT_LOADER_FUNCTIONS = ("load_sql",)


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal(node: ast.expr | None) -> str | None:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else None
    )


def _path_needle(literal: str) -> str | None:
    """A `.sql` path literal as a segment-aligned suffix to compare repo-relative paths against, or None when it
    is a bare filename (handled separately) or not a relative path."""
    needle = re.sub(r"^(?:\.{1,2}/)+", "", literal.replace("\\", "/"))
    return needle if "/" in needle and not needle.startswith("/") else None


class _References:
    def __init__(self, repo_root: Path, loader_functions: frozenset[str]) -> None:
        self.repo_root = repo_root.resolve()
        self.loader_functions = loader_functions
        self.exact: set[tuple[str, str]] = (
            set()
        )  # (topic, name) from fully literal loader calls
        self.dynamic_by_topic: dict[
            str, set[str]
        ] = {}  # topic -> string literals of files with a dynamic-name call
        self.dynamic_any_topic: set[str] = (
            set()
        )  # string literals of files with a dynamic-topic call
        self.path_needles: set[str] = set()
        self.bare_by_file: list[
            tuple[Path, set[str]]
        ] = []  # (python file, string literals ending in .sql)
        self.unparseable: list[Path] = []

    def scan(self, python_files: Iterable[Path]) -> None:
        for path in python_files:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError, OSError, ValueError, RecursionError, MemoryError:
                self.unparseable.append(path)
                continue
            literals = {
                n.value
                for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            }
            sql_literals = {s for s in literals if s.endswith(".sql")}
            self.path_needles.update(n for s in sql_literals if (n := _path_needle(s)))
            if sql_literals:
                self.bare_by_file.append((path.resolve(), sql_literals))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and _call_name(node) in self.loader_functions
                    and (node.args or node.keywords)
                ):
                    self._add_loader_call(node, literals)

    def _add_loader_call(self, node: ast.Call, literals: set[str]) -> None:
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        topic = _literal(node.args[0] if node.args else keywords.get("topic"))
        name = _literal(node.args[1] if len(node.args) > 1 else keywords.get("name"))
        if topic is not None and name is not None:
            self.exact.add((topic, name))
        elif topic is not None:
            self.dynamic_by_topic.setdefault(topic, set()).update(literals - {topic})
        else:
            self.dynamic_any_topic |= literals

    def is_referenced(self, sql_file: Path) -> bool:
        stem, topic = sql_file.stem, sql_file.parent.name
        if (topic, stem) in self.exact:
            return True
        if (
            stem in self.dynamic_by_topic.get(topic, ())
            or stem in self.dynamic_any_topic
        ):
            return True
        resolved = sql_file.resolve()
        rel = resolved.relative_to(self.repo_root).as_posix()
        if any(
            rel == needle or rel.endswith("/" + needle) for needle in self.path_needles
        ):
            return True
        scope = resolved.parent.parent
        if (
            scope == self.repo_root
        ):  # a top-level SQL folder: "its parent" would be the whole repo, tests included
            return False
        return any(
            sql_file.name in lits and py.is_relative_to(scope)
            for py, lits in self.bare_by_file
        )


def check_unreferenced_sql_files(
    *,
    repo_root: Path,
    sql_files: list[Path],
    python_files: Iterable[Path],
    loader_functions: Iterable[str] = DEFAULT_LOADER_FUNCTIONS,
    ignore_globs: Iterable[str] = (),
) -> list[Finding]:
    """One `sql-file-unreferenced` finding per file of `sql_files` (all inside `repo_root`) that no Python file in
    `python_files` references, plus one finding per Python file that could not be parsed."""
    if not sql_files:
        return []
    refs = _References(repo_root, frozenset(loader_functions))
    refs.scan(python_files)
    ignores = list(ignore_globs)
    findings = [
        Finding(
            path,
            0,
            RULE_UNPARSEABLE,
            "could not be parsed, so references to .sql files in it are unknown -- `sql-file-unreferenced` results may be wrong.",
        )
        for path in refs.unparseable
    ]
    for sql_file in sql_files:
        rel = sql_file.resolve().relative_to(refs.repo_root).as_posix()
        if any(fnmatch(rel, g) for g in ignores) or refs.is_referenced(sql_file):
            continue
        findings.append(
            Finding(
                sql_file,
                0,
                RULE,
                "no Python code references this SQL file (load_sql topic/name or path) -- delete it, or list it in "
                "[tool.bdt.dead_code] sql_unreferenced_ignore if it is reached some other way.",
            )
        )
    return findings
