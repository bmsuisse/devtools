"""Read `uv.lock` (PyPI) and `bun.lock` (npm) into `LockedPackage`s.

Only registry packages are returned; git, path, editable and workspace sources are reported as `Unsupported` so a
scan never claims to have looked at a dependency it can't match against an advisory.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .versions import Ecosystem, normalize_name

LOCKFILE_NAMES = {"uv.lock": "PyPI", "bun.lock": "npm"}
_SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", ".git", ".worktrees", "__pycache__", ".tox"})


class LockfileError(ValueError):
    """A lockfile that can't be read: the scan is incomplete, so callers fail instead of reporting "clean"."""


@dataclass(frozen=True, slots=True)
class LockedPackage:
    ecosystem: Ecosystem
    name: str  # normalised (PEP 503 for PyPI)
    version: str
    direct: bool
    lockfile: Path
    declared: str | None = None  # the range/specifier the project itself declares (direct dependencies only)


@dataclass(frozen=True, slots=True)
class Unsupported:
    lockfile: Path
    name: str
    reason: str


@dataclass(slots=True)
class LockfileContent:
    lockfile: Path
    ecosystem: Ecosystem
    packages: list[LockedPackage] = field(default_factory=list)
    unsupported: list[Unsupported] = field(default_factory=list)


def find_lockfiles(roots: Iterable[Path]) -> list[Path]:
    """Every `uv.lock` / `bun.lock` below the roots; `node_modules`, virtualenvs, `.git` and `.worktrees` are not entered
    (a root that itself lives inside one is still scanned)."""
    found: list[Path] = []
    for root in roots:
        if root.is_file() and root.name in LOCKFILE_NAMES:
            found.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            found.extend(Path(dirpath) / name for name in sorted(filenames) if name in LOCKFILE_NAMES)
    return sorted(dict.fromkeys(p.resolve() for p in found))


def read_lockfile(path: Path) -> LockfileContent:
    try:
        return read_uv_lock(path) if path.name == "uv.lock" else read_bun_lock(path)
    except (
        OSError,
        tomllib.TOMLDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise LockfileError(f"{path}: cannot read lockfile ({exc})") from exc


# ── uv.lock ──────────────────────────────────────────────────────────────────────────────────────────────────────


def read_uv_lock(path: Path) -> LockfileContent:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    content = LockfileContent(path, "PyPI")
    direct: dict[str, str | None] = {}
    for pkg in data.get("package", []):
        source = pkg.get("source", {})
        if (
            "virtual" in source or "editable" in source
        ):  # the project itself / a workspace member: its deps are the direct ones
            for dep in pkg.get("dependencies", []):
                direct.setdefault(normalize_name("PyPI", dep["name"]), None)
            for group in (pkg.get("optional-dependencies") or {}).values():
                for dep in group:
                    direct.setdefault(normalize_name("PyPI", dep["name"]), None)
            for group in (pkg.get("dev-dependencies") or {}).values():
                for dep in group:
                    direct.setdefault(normalize_name("PyPI", dep["name"]), None)
            metadata = pkg.get("metadata", {})
            requires = list(metadata.get("requires-dist", []))
            for group in (metadata.get("requires-dev") or {}).values():
                requires.extend(group)
            for req in requires:
                if req.get("specifier"):
                    direct[normalize_name("PyPI", req["name"])] = req["specifier"]
    for pkg in data.get("package", []):
        source = pkg.get("source", {})
        name = pkg["name"]
        if "virtual" in source or "editable" in source:
            continue
        if "registry" not in source:
            content.unsupported.append(
                Unsupported(
                    path,
                    name,
                    f"source is {next(iter(source), 'unknown')}, not a registry",
                )
            )
            continue
        norm = normalize_name("PyPI", name)
        content.packages.append(LockedPackage("PyPI", norm, pkg["version"], norm in direct, path, direct.get(norm)))
    return content


# ── bun.lock ─────────────────────────────────────────────────────────────────────────────────────────────────────


def loads_jsonc(text: str) -> object:
    """`bun.lock` is JSON with trailing commas (and, in principle, comments)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned)


def read_bun_lock(path: Path) -> LockfileContent:
    data = loads_jsonc(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("top level is not an object")
    content = LockfileContent(path, "npm")
    workspaces = cast("dict[str, dict[str, Any]]", data.get("workspaces") or {})
    packages = cast("dict[str, list[Any]]", data.get("packages") or {})
    declared: dict[str, str] = {}
    for workspace in workspaces.values():
        for key in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            for name, spec in (workspace.get(key) or {}).items():
                declared.setdefault(name, spec)
    seen: set[tuple[str, str]] = set()
    for key, entry in packages.items():
        ref = entry[0]
        name, _, version = ref.rpartition("@")
        if not name or "@" in name.lstrip("@") or ":" in version or not re.match(r"^\d", version):
            content.unsupported.append(
                Unsupported(
                    path,
                    ref,
                    "not a plain registry version (alias, git, file or workspace)",
                )
            )
            continue
        if (name, version) in seen:
            continue
        seen.add((name, version))
        is_direct = name in declared and key == name  # a nested `a/b` entry is a transitive copy
        content.packages.append(
            LockedPackage(
                "npm",
                name,
                version,
                is_direct,
                path,
                declared.get(name) if is_direct else None,
            )
        )
    return content
