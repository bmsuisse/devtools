"""`bdt lint`'s tooling-config check (bmsuisse/skills#52): the consuming repo must
declare `ty`, `ruff` and `pytest` and have `pytest` + `prek` (see the `prek` skill)
actually configured. Never a hard block on its own -- see lint.py for how a CLI
flag or `[tool.bdt.lint] skip_tooling_check` bypasses it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .bdt_config import find_pyproject
from .lint_findings import Finding

_REQUIRED_DEPENDENCIES = ("ty", "ruff", "pytest")
_DEP_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _dep_name(spec: str) -> str:
    """`"pgdevkit[db]>=0.7.1"` -> `"pgdevkit"`, `"ty>=0.0.59"` -> `"ty"`."""
    match = _DEP_NAME_RE.match(spec.strip())
    return (match.group(0) if match else spec).lower()


def _declared_dependencies(data: dict) -> set[str]:
    names: set[str] = set()
    project = data.get("project", {}) or {}
    for dep in project.get("dependencies", []) or []:
        names.add(_dep_name(dep))
    for group in (project.get("optional-dependencies") or {}).values():
        for dep in group or []:
            names.add(_dep_name(dep))
    for group in (data.get("dependency-groups") or {}).values():
        for dep in group or []:
            if isinstance(dep, str):
                names.add(_dep_name(dep))
    return names


def check_tooling(root: Path) -> list[Finding]:
    pyproject_path = find_pyproject(root)
    if pyproject_path is None:
        return [
            Finding(
                root,
                0,
                "tooling-missing-pyproject",
                "No pyproject.toml found -- can't verify ty/ruff/pytest are declared/configured.",
            )
        ]

    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    declared = _declared_dependencies(data)
    findings: list[Finding] = []

    for tool in _REQUIRED_DEPENDENCIES:
        if tool not in declared:
            findings.append(
                Finding(
                    pyproject_path,
                    0,
                    f"tooling-missing-{tool}",
                    f"'{tool}' isn't declared as a dependency in pyproject.toml.",
                )
            )

    if "pytest" in declared:
        repo_root = pyproject_path.parent
        pytest_configured = (
            data.get("tool", {}).get("pytest", {}).get("ini_options") is not None
            or (repo_root / "pytest.ini").is_file()
            or (repo_root / "setup.cfg").is_file()
        )
        if not pytest_configured:
            findings.append(
                Finding(
                    pyproject_path,
                    0,
                    "tooling-missing-pytest-config",
                    "pytest is declared but not configured -- add a [tool.pytest.ini_options] table to pyproject.toml "
                    "(or a pytest.ini/setup.cfg).",
                )
            )

    if not (pyproject_path.parent / "prek.toml").is_file():
        findings.append(
            Finding(
                pyproject_path.parent,
                0,
                "tooling-missing-prek",
                "No prek.toml found -- set up prek for formatting/pre-commit hooks (see the `prek` skill).",
            )
        )

    return findings
