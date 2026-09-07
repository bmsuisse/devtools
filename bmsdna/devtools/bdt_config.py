"""Shared lookup for `[tool.bdt.*]` configuration tables in the consuming
repo's pyproject.toml (walks up from a start directory the same way `git`
looks for `.git`, so it works from any subdirectory of the repo).
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def find_pyproject(start: Path | None = None) -> Path | None:
    start = start or Path.cwd()
    for directory in (start, *start.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def load_bdt_table(key: str, start: Path | None = None) -> dict:
    """Load the `[tool.bdt.<key>]` table (e.g. `key="envs"` -> `[tool.bdt.envs]`)."""
    path = find_pyproject(start)
    if path is None:
        return {}
    with path.open("rb") as f:
        data = tomllib.load(f)
    value = data.get("tool", {}).get("bdt", {}).get(key, {})
    return value if isinstance(value, dict) else {}
