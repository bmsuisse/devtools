"""Named environments for `bdt logs fetch`, configured in the consuming repo's
pyproject.toml under [tool.bdt.envs.<name>].

This lets a repo define its webapp/resource-group/slot combinations once
(e.g. "prod", "staging") instead of passing all three flags on every call.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

REQUIRED_KEYS = ("webapp", "resource_group")

CONFIG_EXAMPLE = """\
[tool.bdt.envs.prod]
webapp = "my-app"
resource_group = "my-app-rg"
# slot is optional — omit it for an app's default/production slot,
# set it for a named deployment slot (e.g. "test", "stage").

[tool.bdt.envs.test]
webapp = "my-app"
resource_group = "my-app-rg"
slot = "test"
"""


def _find_pyproject(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def load_envs(start: Path | None = None) -> dict[str, dict[str, str]]:
    path = _find_pyproject(start or Path.cwd())
    if path is None:
        return {}
    with path.open("rb") as f:
        data = tomllib.load(f)
    envs = data.get("tool", {}).get("bdt", {}).get("envs", {})
    return envs if isinstance(envs, dict) else {}


def resolve_env(name: str, start: Path | None = None) -> dict[str, str]:
    """Look up a named environment, exiting with an actionable message if it's missing."""
    envs = load_envs(start)
    if not envs:
        sys.exit(
            "ERROR: no environments configured. Add a [tool.bdt.envs.<name>] table "
            f"to pyproject.toml, e.g.:\n\n{CONFIG_EXAMPLE}"
        )
    if name not in envs:
        available = ", ".join(sorted(envs))
        sys.exit(f"ERROR: unknown --env '{name}'. Configured environments: {available}")

    env = envs[name]
    missing = [k for k in REQUIRED_KEYS if k not in env]
    if missing:
        sys.exit(
            f"ERROR: [tool.bdt.envs.{name}] in pyproject.toml is missing required "
            f"key(s): {', '.join(missing)}"
        )
    return env
