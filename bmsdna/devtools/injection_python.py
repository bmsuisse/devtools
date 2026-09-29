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


_FIRST_ARG_KEYWORDS = ("command", "cmd", "args", "source", "string", "template_source", "data", "s")


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


def _is_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and _is_literal(node.left) and _is_literal(node.right)


def _shell_true(call: ast.Call) -> bool:
    return any(kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in call.keywords)


def _finding(path: Path, node: ast.Call, rule: str, message: str, severity: str) -> Finding:
    return Finding(path, node.lineno, rule, message, severity=severity)


def _check_call(path: Path, node: ast.Call, aliases: dict[str, str]) -> list[Finding]:
    name = _dotted(node.func, aliases)
    last = name.rsplit(".", 1)[-1]
    first_arg = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg in _FIRST_ARG_KEYWORDS), None)
    dynamic = first_arg is not None and not _is_literal(first_arg)

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
    if name.startswith("subprocess.") and _shell_true(node):
        if dynamic or isinstance(first_arg, (ast.JoinedStr, ast.BinOp)):
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
        if name == "yaml.unsafe_load" or loader is None or _dotted(loader, aliases).rsplit(".", 1)[-1] not in _SAFE_YAML_LOADERS:
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
        kw.arg == "autoescape" and isinstance(kw.value, ast.Constant) and kw.value.value is False for kw in node.keywords
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
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            findings.extend(_check_call(path, node, aliases))
    return findings
