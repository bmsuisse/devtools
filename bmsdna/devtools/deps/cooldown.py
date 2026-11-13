"""`bdt deps cooldown`: is a locked or pinned version younger than the dependency cooldown?

uv (`exclude-newer`) and bun (`minimumReleaseAge`) enforce the cooldown while resolving. Gradle has no such setting and
catalogs pin exact versions, so for Maven the cooldown is checked here, against the version a branch adds or changes.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from .lockfiles import LockedPackage, LockfileContent, LockfileError, read_lockfile
from .osv import DataError
from .versions import Ecosystem, normalize_name


@dataclass(frozen=True, slots=True)
class Young:
    package: LockedPackage
    released: datetime
    age_days: float


@dataclass(slots=True)
class CooldownResult:
    young: list[Young] = field(default_factory=list)
    exempt: list[Young] = field(default_factory=list)
    unknown: list[LockedPackage] = field(default_factory=list)  # release time not known (private or unlisted package)
    incomplete: list[str] = field(default_factory=list)
    checked: int = 0


def changed_packages(content: LockfileContent, base: str) -> list[LockedPackage]:
    """The packages of `content` that are not in the same file at git revision `base` (all of them for a new file)."""
    try:
        shown = subprocess.run(
            ["git", "show", f"{base}:./{content.lockfile.name}"],
            cwd=content.lockfile.parent,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise LockfileError(f"cannot run git: {exc}") from exc
    if shown.returncode != 0:
        if "does not exist" in shown.stderr or "exists on disk, but not in" in shown.stderr:
            return list(content.packages)
        raise LockfileError(f"{content.lockfile}: cannot read it at {base}: {shown.stderr.strip()}")
    before = {(p.name, p.version) for p in read_lockfile(content.lockfile, shown.stdout).packages}
    return [p for p in content.packages if (p.name, p.version) not in before]


def check(
    packages: Iterable[LockedPackage],
    release_time: Callable[[Ecosystem, str, str], datetime | None],
    *,
    cooldown_days: float,
    exempt: frozenset[str],
    now: datetime,
) -> CooldownResult:
    result = CooldownResult()
    for pkg in packages:
        result.checked += 1
        try:
            released = release_time(pkg.ecosystem, pkg.name, pkg.version)
        except DataError as exc:
            result.incomplete.append(f"release time of {pkg.name} {pkg.version} unavailable: {exc}")
            continue
        if released is None:
            result.unknown.append(pkg)
            continue
        age = round((now - released).total_seconds() / 86400, 1)
        if age >= cooldown_days:
            continue
        item = Young(pkg, released, age)
        if normalize_name(pkg.ecosystem, pkg.name) in exempt or pkg.name in exempt:
            result.exempt.append(item)
        else:
            result.young.append(item)
    return result
