"""Version parsing and matching for the two supported ecosystems: PyPI (PEP 440) and npm (semver).

A `spec` is a comma/space separated list of comparators (`<4.17.21`, `>=1.0,<2`, `==1.2.3`); declared npm ranges from a
`package.json` (`^4`, `~1.2`, `1.x`, `>=1 <3 || ^5`) go through `npm_range_satisfied`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

Ecosystem = Literal["PyPI", "npm"]
ECOSYSTEMS: tuple[Ecosystem, ...] = ("PyPI", "npm")


class VersionError(ValueError):
    """A version or range string we can't interpret. Callers treat it as "unknown", never as "not affected"."""


_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


@total_ordering
@dataclass(frozen=True, slots=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[int | str, ...] = ()

    def _key(self) -> tuple:
        # semver 2.0: a release sorts after its own prereleases; numeric identifiers sort before alphanumeric ones
        pre = (
            (1,)
            if not self.prerelease
            else (
                0,
                tuple((0, p, "") if isinstance(p, int) else (1, 0, p) for p in self.prerelease),
            )
        )
        return (self.major, self.minor, self.patch, pre)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._key() < other._key()

    def __str__(self) -> str:
        pre = "-" + ".".join(map(str, self.prerelease)) if self.prerelease else ""
        return f"{self.major}.{self.minor}.{self.patch}{pre}"


def parse_semver(text: str) -> SemVer:
    match = _SEMVER_RE.match(text.strip())
    if not match:
        raise VersionError(f"not a semver version: {text!r}")
    major, minor, patch, pre = match.groups()
    prerelease = tuple(int(p) if p.isdigit() else p for p in pre.split(".")) if pre else ()
    return SemVer(int(major), int(minor), int(patch), prerelease)


def parse_version(ecosystem: Ecosystem, text: str) -> Version | SemVer:
    if ecosystem == "PyPI":
        try:
            return Version(text.strip())
        except InvalidVersion as exc:
            raise VersionError(f"not a PEP 440 version: {text!r}") from exc
    return parse_semver(text)


def normalize_name(ecosystem: Ecosystem, name: str) -> str:
    """PEP 503 for PyPI (`Foo_Bar.baz` == `foo-bar-baz`); npm names are case-sensitive and compared as they are."""
    return re.sub(r"[-_.]+", "-", name).lower() if ecosystem == "PyPI" else name


_COMPARATOR_RE = re.compile(r"^(<=|>=|==|!=|<|>|=)\s*(\S+)$")


def spec_matches(ecosystem: Ecosystem, spec: str, version: str) -> bool:
    """Does `version` satisfy every comparator of `spec` (comma or whitespace separated, `,` = and)?"""
    actual = parse_version(ecosystem, version)
    tokens = [t for t in re.split(r"[,\s]+", re.sub(r"(<=|>=|==|!=|<|>|=)\s+", r"\1", spec.strip())) if t]
    if not tokens:
        raise VersionError("empty version spec")
    for token in tokens:
        match = _COMPARATOR_RE.match(token)
        if not match:
            raise VersionError(f"cannot read comparator {token!r} in spec {spec!r}")
        op, bound_text = match.groups()
        bound = parse_version(ecosystem, bound_text)
        ok = {
            "<": actual < bound,
            "<=": actual <= bound,
            ">": actual > bound,
            ">=": actual >= bound,
            "==": actual == bound,
            "=": actual == bound,
            "!=": actual != bound,
        }[op]
        if not ok:
            return False
    return True


def pypi_specifier_satisfied(specifier: str, version: str) -> bool:
    try:
        return SpecifierSet(specifier).contains(version, prereleases=True)
    except (InvalidSpecifier, InvalidVersion) as exc:
        raise VersionError(f"cannot evaluate PEP 440 specifier {specifier!r}: {exc}") from exc


_PARTIAL_RE = re.compile(
    r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def _partial(text: str) -> tuple[int | None, int | None, int | None, str | None]:
    match = _PARTIAL_RE.match(text)
    if not match:
        raise VersionError(f"cannot read npm range part {text!r}")
    parts = [None if p is None or p in "xX*" else int(p) for p in match.groups()[:3]]
    return parts[0], parts[1], parts[2], match.group(4)


def _floor(major: int | None, minor: int | None, patch: int | None, pre: str | None) -> SemVer:
    return parse_semver(f"{major or 0}.{minor or 0}.{patch or 0}" + (f"-{pre}" if pre else ""))


def _comparator_satisfied(version: SemVer, comparator: str) -> bool:
    comparator = comparator.strip()
    if comparator in ("", "*", "x", "X", "latest"):
        return True
    if comparator[0] in "^~":
        marker, rest = comparator[0], comparator[1:].strip()
        major, minor, patch, pre = _partial(rest)
        lower = _floor(major, minor, patch, pre)
        if major is None:
            return True
        if marker == "~":
            upper = SemVer(major, (minor or 0) + 1, 0) if minor is not None else SemVer(major + 1, 0, 0)
        elif major > 0 or minor is None:
            upper = SemVer(major + 1, 0, 0)
        elif minor > 0 or patch is None:
            upper = SemVer(0, minor + 1, 0)
        else:
            upper = SemVer(0, 0, patch + 1)
        return lower <= version < upper
    match = re.match(r"^(<=|>=|<|>|=)?\s*(.+)$", comparator)
    assert match
    op, text = match.group(1) or "=", match.group(2)
    major, minor, patch, pre = _partial(text)
    if op == "=":  # exact or x-range: `1.2` / `1.x` / `1.2.3`
        if patch is not None and minor is not None and major is not None:
            return version == _floor(major, minor, patch, pre)
        if major is None:
            return True
        if minor is None:
            return major == version.major
        return (major, minor) == (version.major, version.minor)
    bound = _floor(major, minor, patch, pre)
    if op == ">":
        if patch is None:  # `>1.2` means >=1.3.0
            bound = (
                SemVer(major + 1, 0, 0)
                if minor is None and major is not None
                else SemVer(major or 0, (minor or 0) + 1, 0)
            )
            return version >= bound
        return version > bound
    if op == "<=":
        if patch is None:  # `<=1.2` means <1.3.0
            upper = (
                SemVer(major + 1, 0, 0)
                if minor is None and major is not None
                else SemVer(major or 0, (minor or 0) + 1, 0)
            )
            return version < upper
        return version <= bound
    return {"<": version < bound, ">=": version >= bound}[op]


def npm_range_satisfied(range_text: str, version: str) -> bool:
    """A `package.json` dependency range (caret, tilde, x-range, comparators, `||`). Tags, URLs, `workspace:`, `npm:`
    aliases and other non-range specs raise VersionError so the caller reports "unknown"."""
    actual = parse_semver(version)
    text = range_text.strip()
    if re.search(r"[:/@]|^[A-Za-z]", text) and text not in ("x", "X"):
        raise VersionError(f"not a version range: {range_text!r}")
    for alternative in text.split("||"):
        # normalise `>= 1.2.3 < 2` / `1.2.3 - 2.3.4` is not supported (hyphen ranges): raise rather than guess
        if re.search(r"\s-\s", alternative):
            raise VersionError(f"hyphen ranges are not supported: {range_text!r}")
        parts = re.split(r"\s+", re.sub(r"(<=|>=|<|>|=|\^|~)\s+", r"\1", alternative.strip()))
        if all(_comparator_satisfied(actual, p) for p in parts if p):
            return True
    return False


def declared_range_satisfied(ecosystem: Ecosystem, declared: str, version: str) -> bool:
    return (
        pypi_specifier_satisfied(declared, version) if ecosystem == "PyPI" else npm_range_satisfied(declared, version)
    )
