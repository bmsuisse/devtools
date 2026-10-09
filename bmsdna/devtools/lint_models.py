"""`bdt lint`'s pydantic-model-placement check (bmsuisse/skills#52): a model with
more than a handful of fields defined inline in an API route/handler file is hard
to reuse and clutters the endpoint -- OneSales and CCMT2 both converge on the same
fix, an `api/models/` (or `schemas/`/`dto/`) subpackage for anything bigger than a
small request/response DTO. This only checks files under an `api/` directory tree
(anywhere else, there's no single convention in these codebases to check against).
"""

from __future__ import annotations

import ast
from pathlib import Path

from .lint_findings import Finding

DEFAULT_FIELD_THRESHOLD = 5
DEFAULT_BASE_CLASS_NAMES = frozenset({"BaseModel", "PostgresTableModel"})
DEFAULT_ALLOWED_SUBDIRS = frozenset({"models", "schemas", "dto"})
DEFAULT_API_DIR_NAMES = frozenset({"api"})


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _is_model_class(node: ast.ClassDef, base_class_names: frozenset[str]) -> bool:
    return any(_base_name(base) in base_class_names for base in node.bases)


def _is_classvar(annotation: ast.expr) -> bool:
    """`ClassVar[...]` fields aren't pydantic model fields -- don't count them."""
    if not isinstance(annotation, ast.Subscript):
        return False
    value = annotation.value
    name = value.id if isinstance(value, ast.Name) else (value.attr if isinstance(value, ast.Attribute) else None)
    return name == "ClassVar"


def _count_fields(node: ast.ClassDef) -> int:
    return sum(
        1
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and not _is_classvar(stmt.annotation)
    )


def _is_disallowed_api_location(rel_dir_parts: tuple[str, ...], api_dir_names: frozenset[str], allowed_subdirs: frozenset[str]) -> bool:
    """True if `rel_dir_parts` (the file's directory components, repo-relative) passes through
    an `api/`-like directory with no `models/`/`schemas/`/`dto/`-like directory after it."""
    for i, part in enumerate(rel_dir_parts):
        if part in api_dir_names:
            return not any(p in allowed_subdirs for p in rel_dir_parts[i + 1 :])
    return False


def check_models_file(
    path: Path,
    *,
    repo_root: Path,
    source: str | None = None,
    field_threshold: int = DEFAULT_FIELD_THRESHOLD,
    base_class_names: frozenset[str] = DEFAULT_BASE_CLASS_NAMES,
    allowed_subdirs: frozenset[str] = DEFAULT_ALLOWED_SUBDIRS,
    api_dir_names: frozenset[str] = DEFAULT_API_DIR_NAMES,
) -> list[Finding]:
    try:
        rel_parts = path.resolve().relative_to(repo_root.resolve()).parts
    except ValueError:
        rel_parts = path.parts
    if not _is_disallowed_api_location(rel_parts[:-1], api_dir_names, allowed_subdirs):
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

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_model_class(node, base_class_names):
            continue
        field_count = _count_fields(node)
        if field_count > field_threshold:
            findings.append(
                Finding(
                    path,
                    node.lineno,
                    "pydantic-model-misplaced",
                    f"'{node.name}' has {field_count} fields (> {field_threshold}) but is defined directly under "
                    "an api/ directory -- move it to an api/models/ (or schemas/, dto/) module instead.",
                )
            )
    return findings
