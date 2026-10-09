"""`bdt find-injection`'s non-SQL Python sinks (bmsuisse/devtools#54): code/command execution
and unsafe-deserialization/markup calls that are only a problem when their input isn't a
literal. SQL is handled by lint_sql (`check_sql_file(review=True)`).

Definite patterns are "error"; anything that depends on where the value comes from is a
"review" item for a human/AI to verify.
"""

from __future__ import annotations

import ast
from pathlib import Path

from .lint_findings import Finding

_SAFE_YAML_LOADERS = frozenset({"SafeLoader", "CSafeLoader", "BaseLoader"})
_MARKUP_FUNCS = frozenset({"Markup", "mark_safe", "format_html_join"})


_FIRST_ARG_KEYWORDS = (
    "command",
    "cmd",
    "args",
    "source",
    "string",
    "template_source",
    "data",
    "s",
)


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> the dotted name it was imported as (`from os import system as sh` -> `sh: os.system`)."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases


def _dotted(func: ast.expr, aliases: dict[str, str]) -> str:
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(aliases.get(func.id, func.id))
    return ".".join(reversed(parts))


class _Scope:
    """Every binding of each name in one function/module scope. `None` marks a binding whose value
    isn't statically known (parameter, loop over a variable, `+=`, import, ...)."""

    def __init__(self, parent: _Scope | None, is_class: bool = False) -> None:
        self.parent = parent
        self.is_class = is_class
        self.bindings: dict[str, list[ast.expr | None]] = {}
        self.global_names: set[str] = set()


def _target_names(target: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]


class _ScopeBuilder(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        self.module = _Scope(None)
        self.scope = self.module
        self.calls: list[tuple[ast.Call, _Scope]] = []
        self._body(tree)

    def _body(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def _bind(self, name: str, value: ast.expr | None) -> None:
        scope = self.module if name in self.scope.global_names else self.scope
        scope.bindings.setdefault(name, []).append(value)

    def _unknown(self, target: ast.AST) -> None:
        for name in _target_names(target):
            self._bind(name, None)

    def _function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    ) -> None:
        if not isinstance(node, ast.Lambda):
            self._bind(node.name, None)
            for deco in node.decorator_list:
                self.visit(deco)
        for default in [
            *node.args.defaults,
            *(d for d in node.args.kw_defaults if d is not None),
        ]:
            self.visit(default)
        outer = self.scope
        parent = outer
        while parent.is_class and parent.parent is not None:
            parent = parent.parent
        self.scope = _Scope(parent)
        args = node.args
        for a in [
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            *(x for x in (args.vararg, args.kwarg) if x),
        ]:
            self._bind(a.arg, None)
        for stmt in node.body if isinstance(node.body, list) else [node.body]:
            self.visit(stmt)
        self.scope = outer

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._bind(node.name, None)
        for expr in [
            *node.bases,
            *(k.value for k in node.keywords),
            *node.decorator_list,
        ]:
            self.visit(expr)
        outer = self.scope
        self.scope = _Scope(outer, is_class=True)
        for stmt in node.body:
            self.visit(stmt)
        self.scope = outer

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.global_names.update(node.names)
        for name in node.names:
            self.module.bindings.setdefault(name, [])

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        scope = self.scope.parent
        while scope is not None and scope is not self.module:
            for name in node.names:
                scope.bindings.setdefault(name, []).append(None)
            scope = scope.parent

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._bind(target.id, node.value)
            else:
                self._unknown(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            if isinstance(node.target, ast.Name):
                self._bind(node.target.id, node.value)
            else:
                self._unknown(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._unknown(node.target)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind(node.target.id, None)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        if isinstance(node.target, ast.Name) and isinstance(
            node.iter, (ast.List, ast.Tuple, ast.Set)
        ):
            for element in node.iter.elts:
                self._bind(node.target.id, element)
        else:
            self._unknown(node.target)
        for stmt in [*node.body, *node.orelse]:
            self.visit(stmt)

    visit_AsyncFor = visit_For

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self._unknown(node.target)
        self._body(node)

    def visit_withitem(self, node: ast.withitem) -> None:
        if node.optional_vars is not None:
            self._unknown(node.optional_vars)
        self._body(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name, None)
        self._body(node)

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self._bind((a.asname or a.name).split(".")[0], None)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for a in node.names:
            self._bind(a.asname or a.name, None)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._unknown(target)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self._bind(node.name, None)
        self._body(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self._bind(node.name, None)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self._bind(node.rest, None)
        self._body(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((node, self.scope))
        self._body(node)


def _lookup(scope: _Scope, name: str) -> list[ast.expr | None] | None:
    current: _Scope | None = scope
    while current is not None:
        if name in current.bindings:
            return current.bindings[name]
        current = current.parent
    return None


def _is_const(
    node: ast.expr, scope: _Scope, seen: frozenset[str] = frozenset()
) -> bool:
    """True when every value `node` can take is a string built only from literals -- including names
    whose every assignment in scope is such a constant."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp):
        return (
            isinstance(node.op, ast.Add)
            and _is_const(node.left, scope, seen)
            and _is_const(node.right, scope, seen)
        )
    if isinstance(node, ast.JoinedStr):
        return all(
            _is_const(v.value if isinstance(v, ast.FormattedValue) else v, scope, seen)
            for v in node.values
        )
    if isinstance(node, ast.IfExp):
        return _is_const(node.body, scope, seen) and _is_const(node.orelse, scope, seen)
    if isinstance(node, ast.Name):
        if node.id in seen:
            return False
        bound = _lookup(scope, node.id)
        return bool(bound) and all(
            b is not None and _is_const(b, scope, seen | {node.id}) for b in bound
        )
    return False


def _is_arg_list(node: ast.expr | None, scope: _Scope) -> bool:
    """A list/tuple command whose program is a literal: extra (even dynamic) items are arguments, not shell code."""
    return (
        isinstance(node, (ast.List, ast.Tuple))
        and bool(node.elts)
        and _is_const(node.elts[0], scope)
    )


def _shell_true(call: ast.Call) -> bool:
    return any(
        kw.arg == "shell"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in call.keywords
    )


def _finding(
    path: Path, node: ast.Call, rule: str, message: str, severity: str
) -> Finding:
    return Finding(path, node.lineno, rule, message, severity=severity)


def _check_call(
    path: Path, node: ast.Call, aliases: dict[str, str], scope: _Scope
) -> list[Finding]:
    name = _dotted(node.func, aliases)
    last = name.rsplit(".", 1)[-1]
    first_arg = (
        node.args[0]
        if node.args
        else next(
            (kw.value for kw in node.keywords if kw.arg in _FIRST_ARG_KEYWORDS), None
        )
    )
    dynamic = first_arg is not None and not _is_const(first_arg, scope)

    if name in ("eval", "exec") and dynamic:
        return [
            _finding(
                path,
                node,
                "py-eval-exec",
                f"`{name}()` on a non-literal executes arbitrary code -- never pass request/user data.",
                "error",
            )
        ]
    if name in ("os.system", "os.popen") and dynamic:
        return [
            _finding(
                path,
                node,
                "py-shell-command",
                f"`{name}()` with a non-literal command runs it through a shell -- use subprocess with an argument list.",
                "error",
            )
        ]
    if (
        name.startswith("subprocess.")
        and _shell_true(node)
        and not _is_arg_list(first_arg, scope)
    ):
        if dynamic:
            return [
                _finding(
                    path,
                    node,
                    "py-shell-command",
                    "`subprocess` with `shell=True` and a non-literal command -- pass an argument list without shell=True.",
                    "error",
                )
            ]
        if first_arg is not None:
            return [
                _finding(
                    path,
                    node,
                    "py-shell-true",
                    "`shell=True` with a literal command is safe today, but verify no value is ever interpolated into it later.",
                    "review",
                )
            ]
    if name in (
        "pickle.loads",
        "pickle.load",
        "cPickle.loads",
        "marshal.loads",
        "shelve.open",
        "dill.loads",
    ):
        return [
            _finding(
                path,
                node,
                "py-unsafe-deserialization",
                f"`{name}()` can execute arbitrary code from untrusted bytes. AI reviewer: verify the data is never user-supplied.",
                "review",
            )
        ]
    if name in ("yaml.load", "yaml.load_all", "yaml.unsafe_load"):
        loader = next((kw.value for kw in node.keywords if kw.arg == "Loader"), None)
        if loader is None and len(node.args) > 1:
            loader = node.args[1]
        if (
            name == "yaml.unsafe_load"
            or loader is None
            or _dotted(loader, aliases).rsplit(".", 1)[-1] not in _SAFE_YAML_LOADERS
        ):
            return [
                _finding(
                    path,
                    node,
                    "py-unsafe-yaml",
                    f"`{name}()` without a safe loader can construct arbitrary objects -- use `yaml.safe_load`.",
                    "error",
                )
            ]
    if last in _MARKUP_FUNCS and dynamic:
        return [
            _finding(
                path,
                node,
                "py-unescaped-markup",
                f"`{last}()` marks a non-literal string as safe HTML (XSS). AI reviewer: verify it is escaped or never user-controlled.",
                "review",
            )
        ]
    if last in ("Environment", "Jinja2Templates") and any(
        kw.arg == "autoescape"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is False
        for kw in node.keywords
    ):
        return [
            _finding(
                path,
                node,
                "py-autoescape-off",
                "Jinja2 with `autoescape=False` -- enable autoescaping to prevent XSS.",
                "error",
            )
        ]
    if last in ("render_template_string", "from_string") and dynamic:
        return [
            _finding(
                path,
                node,
                "py-template-injection",
                f"`{last}()` on a non-literal template allows server-side template injection -- never build the template from user data.",
                "error",
            )
        ]
    return []


def check_python_sinks(path: Path, source: str | None = None) -> list[Finding]:
    if source is None:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    aliases = _import_aliases(tree)
    findings: list[Finding] = []
    for node, scope in _ScopeBuilder(tree).calls:
        findings.extend(_check_call(path, node, aliases, scope))
    return findings
