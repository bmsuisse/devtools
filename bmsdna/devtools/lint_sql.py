"""AST rule engine for `bdt lint`'s postgres/psycopg checks (bmsuisse/skills#52):
every `.execute()`/`.executemany()` call is classified by how its SQL argument was
built, then checked against the `postgres-best-practices` skill's rules
(../../skills/postgres-best-practices in a checkout of bmsuisse/skills).

Only a call whose (resolved) argument text actually parses with `sqlglot` as one
of `_ACCEPTED_STATEMENT_TYPES` is ever flagged -- this is deliberate, not just an
optimization: it's what lets this scan any `.execute()` call, regardless of which
driver it belongs to (psycopg, duckdb, sqlite3, ...), without false-positiving on
non-SQL or non-DML `.execute()` calls (e.g. a duckdb `COPY ... TO` export, which
parses to `exp.Copy`, deliberately outside the accepted set).

Resolution of the query argument is a light, best-effort dataflow: a `Name` is
followed back to every assignment it had in its own scope (function/class/module
scopes tracked separately -- no real branch/loop-aware SSA, but also deliberately
*not* "last assignment wins": each candidate assignment is checked independently,
so an unsafe one a later, safe-looking reassignment happens to shadow at lookup
time is still caught). Anything that resolves to nothing recognizable (a
parameter, a helper-function's return value, ...) is silently skipped rather
than guessed at -- a linter that can't be sure must stay quiet, not noisy.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .lint_findings import Finding

# This module must import cleanly without sqlglot installed -- require_sqlglot() is what gives
# a clear error (with an install hint), not an ImportError at `bdt`'s own startup (cli.py
# imports this unconditionally, for every `bdt` subcommand, not just `bdt lint`).
try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - exercised via require_sqlglot()
    sqlglot = None  # type: ignore
    exp = None  # type: ignore

SQLGLOT_INSTALL_HINT = "Install it: `uv add sqlglot` (or `uv sync --extra lint` if this project depends on bmsdna-devtools[lint])."

# Statement kinds a resolved query argument must parse as for the SQL rules below to apply at
# all -- see the module docstring. `Command`/`Copy`/etc (anything sqlglot can't fully model,
# e.g. duckdb's COPY/ATTACH or postgres's own \-meta-commands) are deliberately excluded.
_ACCEPTED_STATEMENT_TYPES: tuple[type, ...] = ()
if exp is not None:
    _ACCEPTED_STATEMENT_TYPES = (exp.Select, exp.Insert, exp.Update, exp.Delete, exp.Union, exp.Merge)

# Same pattern prek's check_files.py forbids in .sql files -- kept in sync deliberately, applied
# here to inline SQL literals too.
_FORBIDDEN_JOIN_RE = re.compile(r"(?i)(RIGHT\s+(OUTER\s+)?JOIN|JOIN\s+LATERAL|LATERAL\s+(OUTER\s+)?JOIN|CROSS\s+APPLY)")

# A bare `%s` positional placeholder -- but not `%%s` (psycopg's escape for a literal '%s' in
# the SQL text, e.g. inside a LIKE pattern) and not the `s` in `%(name)s`, which never has `%s`
# as a substring immediately at the `%`.
_POSITIONAL_PARAM_RE = re.compile(r"(?<!%)%s\b")

# Placeholder substituted for a non-literal fragment (an f-string {expr}, a concatenated
# non-constant operand, a str.format() {field}) before attempting an sqlglot parse -- padded
# with spaces so it never merges into a neighboring identifier/keyword, and looks like an
# ordinary identifier so the parse doesn't fail on the substitution itself.
_PROBE_PLACEHOLDER = " __X__ "
_FORMAT_FIELD_RE = re.compile(r"\{[^{}]*\}")

_MAX_RESOLVE_HOPS = 5


def require_sqlglot() -> None:
    """Mirrors `cli_tools.require_tool`'s `sys.exit(message)` convention (a clear message +
    install hint, not a raw traceback) for a missing *package* rather than a missing binary."""
    if sqlglot is None:
        sys.exit(f"'sqlglot' is required for `bdt lint`'s SQL checks but isn't installed.\n{SQLGLOT_INSTALL_HINT}")


def parse_sql_text(text: str, dialect: str = "postgres") -> "exp.Expression | None":
    """`text` parsed as one statement, or None if it fails to parse or doesn't parse as one of
    `_ACCEPTED_STATEMENT_TYPES` -- the gate that makes every rule below "pretty sure it's really
    SQL" before applying (see module docstring)."""
    if sqlglot is None or not text.strip():
        return None
    try:
        parsed = sqlglot.parse_one(text, dialect=dialect)
    except Exception:
        return None
    if parsed is None or not isinstance(parsed, _ACCEPTED_STATEMENT_TYPES):
        return None
    return parsed


def _is_load_sql_call(node: ast.AST) -> bool:
    """`load_sql(...)` (postgres-best-practices skill's `db/loader.py` convention) or
    `<anything>.load_sql(...)` (pgdevkit's `SqlLoader.load_sql`)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "load_sql"
    return isinstance(func, ast.Attribute) and func.attr == "load_sql"


def _is_sql_composed_call(node: ast.AST) -> bool:
    """`sql.SQL(...)`/`sql.Identifier(...)`/`sql.Composed(...)` -- the dynamic-SQL escape hatch
    for Python < 3.14 (psycopg's `sql` module; see postgres-best-practices/references/dynamic-sql.md).
    Trusted outright: its whole purpose is safe composition, so it isn't re-checked here.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
    return name in ("SQL", "Identifier", "Composed")


_UNWRAP_METHODS = frozenset({"strip", "lstrip", "rstrip"})
_UNWRAP_FUNCS = frozenset({"dedent", "cleandoc"})


@dataclass(frozen=True, slots=True)
class _Trust:
    """What a file establishes as safe-by-construction SQL producers: local names bound from
    `sqlglot` imports, and functions defined in the file with a `-> LiteralString` return type."""

    sqlglot_names: frozenset[str] = frozenset()
    literal_funcs: frozenset[str] = frozenset()


_NO_TRUST = _Trust()


def _call_root_name(func: ast.expr) -> str | None:
    while isinstance(func, (ast.Attribute, ast.Call)):
        func = func.value if isinstance(func, ast.Attribute) else func.func
    return func.id if isinstance(func, ast.Name) else None


def _is_literal_string_ref(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "LiteralString") or (isinstance(node, ast.Attribute) and node.attr == "LiteralString")


def _is_trusted_sql_call(node: ast.AST, trust: _Trust = _NO_TRUST) -> bool:
    """A call whose result is SQL that's safe by construction: a sqlglot expression (built from a
    name imported from `sqlglot`, or rendered with `.sql()` in a file that imports sqlglot),
    `cast(LiteralString, ...)` (the author asserts it's static), or a function defined in the same
    file with a `-> LiteralString` return type (called by name, or as `self.`/`cls.`)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if trust.sqlglot_names:
        if isinstance(func, ast.Attribute) and func.attr == "sql":
            return True
        if _call_root_name(func) in trust.sqlglot_names:
            return True
    if isinstance(func, ast.Name):
        return func.id in trust.literal_funcs
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in ("self", "cls")
        and func.attr in trust.literal_funcs
    )


def _literal_string_cast_inner(node: ast.Call) -> ast.expr | None:
    """`cast(LiteralString, x)` -> `x`. The cast itself proves nothing: it is only as safe as `x`."""
    func = node.func
    is_cast = (isinstance(func, ast.Name) and func.id == "cast") or (isinstance(func, ast.Attribute) and func.attr == "cast")
    if is_cast and len(node.args) == 2 and _is_literal_string_ref(node.args[0]):
        return node.args[1]
    return None


def _unwrap_str_call(node: ast.Call) -> ast.expr | None:
    """`textwrap.dedent(x)` / `x.strip()` -> `x`: these don't change whether the text is safe."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _UNWRAP_METHODS and not node.args:
        return func.value
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if name in _UNWRAP_FUNCS and len(node.args) == 1:
        return node.args[0]
    return None


def _file_trust(tree: ast.AST) -> _Trust:
    literal_funcs: set[str] = set()
    sqlglot_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None and _is_literal_string_ref(node.returns):
            literal_funcs.add(node.name)
        elif isinstance(node, ast.Import):
            sqlglot_names.update((a.asname or a.name).split(".")[0] for a in node.names if a.name.split(".")[0] == "sqlglot")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "sqlglot":
            sqlglot_names.update(a.asname or a.name for a in node.names)
    return _Trust(frozenset(sqlglot_names), frozenset(literal_funcs))


def _resolve_single(expr: ast.expr, lookup) -> ast.expr:
    """Follow a `Name` back through `lookup` up to `_MAX_RESOLVE_HOPS` times, taking the
    *first* candidate assignment at each hop. Used only for secondary/nested resolutions (the
    left side of a `%`-format, a `.format()` template, a concat operand) where collapsing
    multiple candidates to one is an acceptable simplification -- the call argument itself
    (the case that actually matters for injection detection) goes through
    `_resolve_candidates` instead, which doesn't collapse anything."""
    seen = 0
    while isinstance(expr, ast.Name) and seen < _MAX_RESOLVE_HOPS:
        bound = lookup(expr.id)
        if not bound:
            break
        expr = bound[0]
        seen += 1
    return expr


def _resolve_candidates(expr: ast.expr, lookup, _depth: int = 0) -> list[ast.expr]:
    """Every value `expr` could resolve to, following `Name` -> assignment(s) through
    `lookup`. A name assigned more than once in its scope (e.g. once per `if`/`else` branch)
    yields one candidate per assignment -- deliberately not just the textually-last one: a
    linter that only checked the last assignment would miss an unsafe branch that a later,
    safe-looking reassignment (in another branch) happens to shadow at lookup time. Every
    candidate is checked independently by `_check_query_arg`, so an unsafe one anywhere is
    still caught regardless of which branch actually runs.
    """
    if isinstance(expr, ast.Name) and _depth < _MAX_RESOLVE_HOPS:
        bound = lookup(expr.id)
        if not bound:
            return [expr]
        results: list[ast.expr] = []
        for candidate in bound:
            results.extend(_resolve_candidates(candidate, lookup, _depth + 1))
        return results
    return [expr]


def _fstring_probe_text(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append(_PROBE_PLACEHOLDER)
    return "".join(parts)


def _concat_probe_text(node: ast.BinOp, lookup) -> str | None:
    """Flattens a chain of `+` string concatenation into probe text, substituting
    `_PROBE_PLACEHOLDER` for any non-literal operand. Returns None if no operand at all is a
    string literal (i.e. this is very unlikely to be SQL text -- e.g. plain numeric addition)."""
    parts: list[str] = []
    saw_literal = False

    def visit(operand: ast.expr) -> None:
        nonlocal saw_literal
        if isinstance(operand, ast.BinOp) and isinstance(operand.op, ast.Add):
            visit(operand.left)
            visit(operand.right)
            return
        resolved = _resolve_single(operand, lookup)
        if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
            parts.append(resolved.value)
            saw_literal = True
        else:
            parts.append(_PROBE_PLACEHOLDER)

    visit(node)
    return "".join(parts) if saw_literal else None


def _injection_finding(text: str, path: Path, lineno: int, rule: str, how: str) -> list[Finding]:
    if parse_sql_text(text) is None:
        return []
    return [
        Finding(
            path,
            lineno,
            rule,
            f"SQL built with {how} -- injection risk. Use a psycopg t-string (Python 3.14+), "
            "psycopg.sql for dynamic SQL, or load_sql()/a .sql file for static SQL.",
        )
    ]


def _literal_findings(text: str, path: Path, lineno: int) -> list[Finding]:
    parsed = parse_sql_text(text)
    if parsed is None:
        return []
    findings: list[Finding] = []

    line_count = len([line for line in text.splitlines() if line.strip()])
    is_complex = (
        line_count > 4
        or parsed.find(exp.Join) is not None
        or parsed.find(exp.With) is not None
        or parsed.find(exp.Subquery) is not None
        or parsed.find(exp.AggFunc) is not None
    )
    if is_complex:
        findings.append(
            Finding(
                path,
                lineno,
                "sql-inline-too-complex",
                "Inline SQL is more than a trivial (<=4 line) query, or contains a JOIN/CTE/subquery/aggregation "
                "-- move it to its own .sql file and load it with load_sql()/SqlLoader.",
            )
        )

    if _POSITIONAL_PARAM_RE.search(text):
        findings.append(
            Finding(
                path,
                lineno,
                "sql-positional-param",
                "Uses positional `%s` params -- use named `%(name)s` params with a dict argument instead.",
            )
        )

    join_match = _FORBIDDEN_JOIN_RE.search(text)
    if join_match:
        findings.append(
            Finding(
                path,
                lineno,
                "sql-forbidden-join",
                f"Forbidden join pattern '{join_match.group(0)}' -- avoid LATERAL/RIGHT JOIN and CROSS APPLY; "
                "rewrite as a CTE that pre-aggregates, then join it.",
            )
        )

    return findings


def _check_resolved_candidate(
    resolved: ast.expr,
    lookup,
    path: Path,
    lineno: int,
    trust: _Trust = _NO_TRUST,
    review: bool = False,
) -> list[Finding]:
    if _is_load_sql_call(resolved) or _is_sql_composed_call(resolved) or _is_trusted_sql_call(resolved, trust):
        return []
    if isinstance(resolved, getattr(ast, "TemplateStr", ())):
        return []  # psycopg t-string: params are always bound, never interpolated

    if isinstance(resolved, ast.JoinedStr):
        return _injection_finding(_fstring_probe_text(resolved), path, lineno, "sql-fstring-injection", "an f-string")

    if isinstance(resolved, ast.BinOp) and isinstance(resolved.op, ast.Mod):
        left = _resolve_single(resolved.left, lookup)
        if isinstance(left, ast.Constant) and isinstance(left.value, str):
            return _injection_finding(left.value, path, lineno, "sql-percent-format-injection", "the `%` string-formatting operator")
        return []

    if isinstance(resolved, ast.BinOp) and isinstance(resolved.op, ast.Add):
        text = _concat_probe_text(resolved, lookup)
        return _injection_finding(text, path, lineno, "sql-concat-injection", "string concatenation") if text is not None else []

    if isinstance(resolved, ast.Call) and isinstance(resolved.func, ast.Attribute) and resolved.func.attr == "format":
        template = _resolve_single(resolved.func.value, lookup)
        if isinstance(template, ast.Constant) and isinstance(template.value, str):
            text = _FORMAT_FIELD_RE.sub(_PROBE_PLACEHOLDER, template.value)
            return _injection_finding(text, path, lineno, "sql-format-injection", "`str.format()`")
        return []

    if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
        return _literal_findings(resolved.value, path, lineno)

    if isinstance(resolved, ast.Call):
        cast_inner = _literal_string_cast_inner(resolved)
        if cast_inner is not None:
            findings = _check_query_arg(cast_inner, lookup, path, lineno, trust, review)
            if review and not findings and any(isinstance(c, (ast.Name, ast.Attribute, ast.Subscript)) for c in _resolve_candidates(cast_inner, lookup)):
                findings = [_unverified_cast_finding(path, lineno)]
            return findings
        inner = _unwrap_str_call(resolved)
        if inner is not None:
            return _check_query_arg(inner, lookup, path, lineno, trust, review)
        if review:
            return [_unverified_call_finding(resolved, path, lineno)]

    return []  # unresolved (a parameter, a helper-function result, ...) -- stay quiet


def _unverified_call_finding(call: ast.Call, path: Path, lineno: int) -> Finding:
    return Finding(
        path,
        lineno,
        "sql-unverified-call",
        f"SQL text comes from `{ast.unparse(call.func)}(...)`, which isn't load_sql()/sql.SQL/sqlglot/a `-> LiteralString` function. "
        "AI reviewer: verify that no user-controlled value can reach this query (injection risk); if it is safe, "
        "type the producer `-> LiteralString`, build it with sqlglot, or add `# bdt-lint: ignore sql-unverified-call`.",
        severity="review",
    )


def _unverified_cast_finding(path: Path, lineno: int) -> Finding:
    return Finding(
        path,
        lineno,
        "sql-unverified-cast",
        "`cast(LiteralString, ...)` wraps a value that isn't visibly a sqlglot expression or a literal. AI reviewer: verify it "
        "cannot carry user-controlled text (the cast doesn't make it safe); pass a sqlglot expression, or add "
        "`# bdt-lint: ignore sql-unverified-cast`.",
        severity="review",
    )


def _check_query_arg(
    expr: ast.expr,
    lookup,
    path: Path,
    lineno: int,
    trust: _Trust = _NO_TRUST,
    review: bool = False,
) -> list[Finding]:
    """Checks every candidate `expr` could resolve to (see `_resolve_candidates`) and returns
    the union of findings, deduplicated by rule -- so a name reassigned differently per branch
    (e.g. an unsafe default that one `if` branch overwrites with a safe literal) is still
    caught via whichever branch is unsafe, not silently cleared by whichever assignment
    happens to be lexically last.
    """
    findings: list[Finding] = []
    seen_rules: set[str] = set()
    for candidate in _resolve_candidates(expr, lookup):
        for finding in _check_resolved_candidate(candidate, lookup, path, lineno, trust, review):
            if finding.rule not in seen_rules:
                seen_rules.add(finding.rule)
                findings.append(finding)
    return findings


def _execute_query_arg(call: ast.Call) -> ast.expr | None:
    if not (isinstance(call.func, ast.Attribute) and call.func.attr in ("execute", "executemany")):
        return None
    if call.args:
        return call.args[0]
    return next((kw.value for kw in call.keywords if kw.arg == "query"), None)


class _ExecuteCallVisitor(ast.NodeVisitor):
    """Walks a module, tracking a stack of (function/class/module) local-variable scopes so a
    `cur.execute(query, ...)` call can resolve `query` back to every assignment it had in that
    scope (see `_resolve_candidates` -- deliberately not just the nearest/last one, so a
    branch-shadowed unsafe assignment is still seen), then applies the SQL rules to every
    `.execute()`/`.executemany()` call found."""

    def __init__(self, path: Path, trust: _Trust = _NO_TRUST, review: bool = False) -> None:
        self.path = path
        self.trust = trust
        self.review = review
        self.findings: list[Finding] = []
        self._scopes: list[dict[str, list[ast.expr]]] = [{}]

    def _lookup(self, name: str) -> list[ast.expr]:
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return []

    def _visit_scoped(self, node: ast.AST) -> None:
        self._scopes.append({})
        self.generic_visit(node)
        self._scopes.pop()

    visit_FunctionDef = _visit_scoped
    visit_AsyncFunctionDef = _visit_scoped
    visit_ClassDef = _visit_scoped

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._scopes[-1].setdefault(node.targets[0].id, []).append(node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        query_arg = _execute_query_arg(node)
        if query_arg is not None:
            self.findings.extend(_check_query_arg(query_arg, self._lookup, self.path, node.lineno, self.trust, self.review))
        self.generic_visit(node)


def check_sql_file(path: Path, source: str | None = None, *, review: bool = False) -> list[Finding]:
    """Every SQL-rule finding for one Python file. `source` lets callers pass already-read
    content (e.g. from a staged-file snapshot); defaults to reading `path`. `review=True`
    (`bdt find-injection`) additionally reports queries produced by an unverifiable function call."""
    if sqlglot is None:
        return []
    if source is not None:
        text = source
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []  # not a readable UTF-8 Python file -- skip it like a syntax error
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    visitor = _ExecuteCallVisitor(path, _file_trust(tree), review)
    visitor.visit(tree)
    return visitor.findings
