"""Read `uv.lock` (PyPI), `bun.lock` (npm), `gradle.lockfile` and Gradle version catalogs (Maven) into `LockedPackage`s.

Only registry packages are returned; git, path, editable and workspace sources (and Gradle versions a catalog does not pin)
are reported as `Unsupported` so a scan never claims to have looked at a dependency it can't match against an advisory.
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

LOCKFILE_NAMES = {"uv.lock": "PyPI", "bun.lock": "npm", "gradle.lockfile": "Maven"}
_SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", ".git", ".worktrees", "__pycache__", ".tox", ".gradle"})


def is_lockfile(path: Path) -> bool:
    """A lockfile by name, or a Gradle version catalog (`gradle/*.versions.toml`: the pins of a project without locking)."""
    return path.name in LOCKFILE_NAMES or (path.name.endswith(".versions.toml") and path.parent.name == "gradle")


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
    """Every `uv.lock` / `bun.lock` / `gradle.lockfile` / `gradle/*.versions.toml` below the roots; `node_modules`,
    virtualenvs, `.git`, `.gradle` and `.worktrees` are not entered (a root that itself lives inside one is still scanned)."""
    found: list[Path] = []
    errors: list[str] = []
    for root in roots:
        if root.is_file() and is_lockfile(root):
            found.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(
            root, onerror=lambda exc: errors.append(f"{exc.filename}: {exc.strerror}")
        ):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            found.extend(Path(dirpath) / name for name in sorted(filenames) if is_lockfile(Path(dirpath) / name))
    if errors:  # an unreadable directory may hide a lockfile: the scan is incomplete, not clean
        raise LockfileError(f"cannot read directory ({'; '.join(errors)})")
    return sorted(dict.fromkeys(p.resolve() for p in found))


def read_lockfile(path: Path, text: str | None = None) -> LockfileContent:
    """Parse the lockfile at `path`; `text` is its content when it was not read from disk (an older git revision)."""
    try:
        if path.name == "uv.lock":
            return read_uv_lock(path, text)
        if path.name == "gradle.lockfile":
            return read_gradle_lockfile(path, text)
        if path.name.endswith(".versions.toml"):
            return read_gradle_catalog(path, text)
        return read_bun_lock(path, text)
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


def read_uv_lock(path: Path, text: str | None = None) -> LockfileContent:
    data = tomllib.loads(text if text is not None else path.read_text(encoding="utf-8"))
    if not isinstance(data.get("package"), list) or not data["package"]:
        raise ValueError("no [[package]] entries: not a uv lockfile")
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


def read_bun_lock(path: Path, text: str | None = None) -> LockfileContent:
    data = loads_jsonc(text if text is not None else path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("packages"), dict)
        or not isinstance(data.get("workspaces"), dict)
    ):
        raise ValueError('"workspaces" and "packages" objects are missing: not a bun lockfile')
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


# ── Gradle: gradle.lockfile and version catalogs ─────────────────────────────────────────────────────────────────

_GRADLE_RANGE_RE = re.compile(r"^[\[(]|[\])]$|\+$|^latest\.")


def _catalog_version(spec: object, versions: dict[str, Any]) -> tuple[str | None, str]:
    """The exact version a catalog entry pins, or None and the reason it can't be matched."""
    if isinstance(spec, dict):
        table = cast("dict[str, Any]", spec)
        if "ref" in table:
            return _catalog_version(versions.get(table["ref"]), versions)
        spec = table.get("strictly") or table.get("require") or table.get("prefer")
    if not isinstance(spec, str) or not spec:
        return (None, "no version in the catalog (managed by a BOM or platform, or by the build script)")
    if _GRADLE_RANGE_RE.search(spec):
        return (None, f"version {spec!r} is a range or dynamic selector, not an exact version")
    return spec, ""


def read_gradle_catalog(path: Path, text: str | None = None) -> LockfileContent:
    """`gradle/libs.versions.toml`: libraries and plugins with their pinned versions. Direct dependencies only (transitive
    ones need Gradle dependency locking); entries without a version are `Unsupported`."""
    data = tomllib.loads(text if text is not None else path.read_text(encoding="utf-8"))
    if not isinstance(data.get("libraries"), dict) and not isinstance(data.get("plugins"), dict):
        raise ValueError("no [libraries] or [plugins] table: not a Gradle version catalog")
    versions = cast("dict[str, Any]", data.get("versions") or {})
    content = LockfileContent(path, "Maven")
    seen: set[tuple[str, str]] = set()

    def add(name: str, spec: object) -> None:
        version, reason = _catalog_version(spec, versions)
        if version is None:
            content.unsupported.append(Unsupported(path, name, reason))
        elif (name, version) not in seen:
            seen.add((name, version))
            content.packages.append(LockedPackage("Maven", name, version, True, path))

    for entry in (data.get("libraries") or {}).values():
        if isinstance(entry, str):
            parts = entry.split(":")
            name, spec = ":".join(parts[:2]), (parts[2] if len(parts) > 2 else None)
        else:
            module = entry.get("module") or f"{entry.get('group', '')}:{entry.get('name', '')}"
            name, spec = module, entry.get("version")
        if name.count(":") != 1 or not all(name.split(":")):
            raise ValueError(f"cannot read library coordinates {name!r}")
        add(name, spec)
    for entry in (data.get("plugins") or {}).values():
        if isinstance(entry, str):
            plugin_id, _, spec = entry.partition(":")
        else:
            plugin_id, spec = entry["id"], entry.get("version")
        add(f"{plugin_id}:{plugin_id}.gradle.plugin", spec)  # the plugin marker artifact
    return content


def read_gradle_lockfile(path: Path, text: str | None = None) -> LockfileContent:
    """`gradle.lockfile` (dependency locking): one `group:artifact:version=configuration,...` line per resolved module."""
    direct: set[str] = set()
    for catalog in (path.parent / "gradle").glob("*.versions.toml") if (path.parent / "gradle").is_dir() else ():
        direct.update(p.name for p in read_gradle_catalog(catalog).packages)
    content = LockfileContent(path, "Maven")
    seen: set[tuple[str, str]] = set()
    for raw in (text if text is not None else path.read_text(encoding="utf-8")).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("empty="):
            continue
        coordinates = line.partition("=")[0].split(":")
        if len(coordinates) != 3 or not all(coordinates):
            raise ValueError(f"cannot read line {line!r}")
        name, version = ":".join(coordinates[:2]), coordinates[2]
        if (name, version) not in seen:
            seen.add((name, version))
            content.packages.append(LockedPackage("Maven", name, version, name in direct, path))
    return content
