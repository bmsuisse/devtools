"""Advisory evaluation for `bdt deps advisories`: OSV records in, one status per affected locked package out.

Statuses (the maintenance skill's cooldown exception flow, references/audit.md, acts on them):

- `malicious`       a `MAL-` advisory covers the locked version: an incident, not a bump
- `needs-triage`    affected range, fix or (when the fix is younger than the cooldown) severity is missing, conflicting
                    or unparseable: a human decides, never an automatic exception
- `no-fix`          an affected advisory has no fixed version
- `blocked`         the fixed version is outside the range the project itself declares for a direct dependency
- `exempt-eligible` high/critical (or in CISA KEV) and the fixed version is younger than the cooldown (or its age is unknown)
- `bump`            the fixed version is already older than the cooldown: a normal bump takes it
- `informational`   affected, lower severity, fix still inside the cooldown: wait

The target is the lowest version the advisory data shows to be outside every affected range of the urgent advisories
(all of them when none is urgent), never "latest". Compatibility with parent ranges of *transitive* dependencies is not
known here; re-run after the bump to confirm the locked version moved.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .lockfiles import LockedPackage, LockfileContent, Unsupported
from .osv import AdvisorySource, DataError, PackageQuery
from .versions import (
    Ecosystem,
    SemVer,
    VersionError,
    declared_range_satisfied,
    normalize_name,
    parse_version,
)

Status = Literal[
    "malicious",
    "needs-triage",
    "no-fix",
    "blocked",
    "exempt-eligible",
    "bump",
    "informational",
]
URGENT_LABELS = frozenset({"HIGH", "CRITICAL"})
_SEVERITY_RANK = {"LOW": 0, "MODERATE": 1, "HIGH": 2, "CRITICAL": 3}
DEFAULT_COOLDOWN_DAYS = 5


@dataclass(frozen=True, slots=True)
class AffectedEntry:
    ecosystem: str
    name: str
    ranges: tuple[tuple[str, tuple[dict[str, str], ...]], ...]  # (range type, events)
    versions: frozenset[str]


@dataclass(frozen=True, slots=True)
class Vuln:
    id: str
    aliases: frozenset[str]
    summary: str
    severity: str | None  # GHSA label: LOW / MODERATE / HIGH / CRITICAL
    affected: tuple[AffectedEntry, ...]
    cvss: tuple[float, ...] = ()  # CVSS v3 base scores computed from OSV `severity[]` vectors


def parse_vuln(raw: dict[str, Any]) -> Vuln:
    label = ((raw.get("database_specific") or {}).get("severity") or "").upper() or None
    if label == "MEDIUM":
        label = "MODERATE"
    entries = []
    for aff in raw.get("affected", []):
        package = aff.get("package") or {}
        eco = package.get("ecosystem", "")
        name = normalize_name("PyPI", package.get("name", "")) if eco == "PyPI" else package.get("name", "")
        ranges = tuple((r.get("type", ""), tuple(r.get("events", []))) for r in aff.get("ranges", []))
        entries.append(AffectedEntry(eco, name, ranges, frozenset(aff.get("versions", []))))
    scores = tuple(
        score
        for score in (
            cvss3_score(e.get("score", ""))
            for e in raw.get("severity", [])
            if e.get("type") in ("CVSS_V3", "CVSS_V3_1")
        )
        if score is not None
    )
    return Vuln(
        raw["id"],
        frozenset(raw.get("aliases", [])),
        raw.get("summary") or raw.get("details", "")[:120],
        label,
        tuple(entries),
        scores,
    )


_CVSS_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_CVSS_AC = {"L": 0.77, "H": 0.44}
_CVSS_UI = {"N": 0.85, "R": 0.62}
_CVSS_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}


def _roundup(value: float) -> float:
    scaled = round(value * 100000)
    return scaled / 100000.0 if scaled % 10000 == 0 else (scaled // 10000 + 1) / 10.0


def cvss3_score(vector: str) -> float | None:
    """Base score of a CVSS 3.0/3.1 vector (FIRST specification); None for anything else (v2, v4, malformed)."""
    if not vector.startswith(("CVSS:3.0/", "CVSS:3.1/")):
        return None
    try:
        m = dict(part.split(":", 1) for part in vector.split("/")[1:])
        if m["S"] not in ("U", "C"):
            return None
        changed = m["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[m["PR"]]
        iss = 1 - (1 - _CVSS_CIA[m["C"]]) * (1 - _CVSS_CIA[m["I"]]) * (1 - _CVSS_CIA[m["A"]])
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
        exploitability = 8.22 * _CVSS_AV[m["AV"]] * _CVSS_AC[m["AC"]] * pr * _CVSS_UI[m["UI"]]
    except KeyError, ValueError:
        return None
    if impact <= 0:
        return 0.0
    return _roundup(min((1.08 if changed else 1.0) * (impact + exploitability), 10))


def _band(score: float) -> str:
    return "CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MODERATE" if score >= 4 else "LOW"


# ── fixed version of one advisory ────────────────────────────────────────────────────────────────────────────────

FixState = Literal["fixed", "no-fix", "unknown"]


def _event_version(ecosystem: Ecosystem, text: str) -> Any:
    if text == "0":
        return SemVer(0, 0, 0) if ecosystem == "npm" else parse_version(ecosystem, "0")
    return parse_version(ecosystem, text)


def fix_for(vuln: Vuln, ecosystem: Ecosystem, name: str, version: str) -> tuple[FixState, str | None, str]:
    """The lowest version outside every affected interval of `vuln` that contains the locked `version`."""
    try:
        locked = parse_version(ecosystem, version)
    except VersionError as exc:
        return "unknown", None, str(exc)
    entries = [a for a in vuln.affected if a.ecosystem == ecosystem and a.name == name]
    if not entries:
        return "unknown", None, "the advisory has no affected entry for this package"
    fixed: list[tuple[Any, str]] = []
    open_ended = False
    try:
        for entry in entries:
            for range_type, events in entry.ranges:
                if range_type not in ("ECOSYSTEM", "SEMVER"):
                    continue
                parsed = []
                for event in events:
                    for kind in ("introduced", "fixed", "last_affected"):
                        if kind in event:
                            parsed.append(
                                (
                                    _event_version(ecosystem, event[kind]),
                                    kind,
                                    event[kind],
                                )
                            )
                order = {"introduced": 0, "last_affected": 1, "fixed": 2}
                parsed.sort(key=lambda e: (e[0], order[e[1]]))
                start = None
                for ver, kind, text in parsed:
                    if kind == "introduced":
                        start = ver
                    elif start is None:
                        continue
                    elif kind == "fixed":
                        if start <= locked < ver:
                            fixed.append((ver, text))
                        start = None
                    else:  # last_affected: the interval is closed but names no fix
                        open_ended = open_ended or start <= locked <= ver
                        start = None
                if start is not None and start <= locked:
                    open_ended = True  # introduced with no upper bound
    except VersionError as exc:
        return "unknown", None, f"cannot interpret the affected ranges: {exc}"
    if fixed:
        return "fixed", max(fixed, key=lambda f: f[0])[1], ""
    if open_ended:
        return "no-fix", None, "the advisory lists no fixed version"
    return (
        "unknown",
        None,
        "the locked version is not inside any affected range the advisory describes",
    )


# ── grouping aliases (GHSA-x = PYSEC-y = CVE-z) ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class VulnGroup:
    vulns: list[Vuln]

    @property
    def ids(self) -> list[str]:
        return sorted(v.id for v in self.vulns)

    @property
    def cves(self) -> set[str]:
        return {a.upper() for v in self.vulns for a in (v.aliases | {v.id}) if a.upper().startswith("CVE-")}

    @property
    def malicious(self) -> bool:
        return any(v.id.startswith("MAL-") for v in self.vulns)

    @property
    def severity_labels(self) -> set[str]:
        """The GHSA labels and the bands of the CVSS v3 scores of every member record."""
        return {v.severity for v in self.vulns if v.severity} | {_band(sc) for v in self.vulns for sc in v.cvss}

    @property
    def summary(self) -> str:
        return next((v.summary for v in self.vulns if v.summary), "")


def group_vulns(vulns: Iterable[Vuln]) -> list[VulnGroup]:
    items = list(vulns)
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, v in enumerate(items):
        for key in {v.id, *v.aliases}:
            if key in owner:
                parent[find(i)] = find(owner[key])
            else:
                owner[key] = i
    groups: dict[int, VulnGroup] = {}
    for i, v in enumerate(items):
        groups.setdefault(find(i), VulnGroup([])).vulns.append(v)
    return list(groups.values())


# ── per package ──────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class GroupInfo:
    ids: list[str]
    cves: list[str]
    severity: str  # label, "unknown" or "conflicting"
    kev: bool
    fixed: str | None
    summary: str


@dataclass(slots=True)
class Finding:
    package: LockedPackage
    status: Status
    reason: str
    groups: list[GroupInfo] = field(default_factory=list)
    target: str | None = None
    fix_age_days: float | None = None
    commands: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    unsupported: list[Unsupported] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)  # reasons the answer is not complete (exit code 3)
    scanned: int = 0


def _commands(finding: Finding) -> list[str]:
    pkg, target = finding.package, finding.target
    if finding.status not in ("exempt-eligible", "bump") or not target:
        return []
    project = pkg.lockfile.parent.parent if pkg.lockfile.parent.name == "gradle" else pkg.lockfile.parent
    where = f"cd {project}"
    if pkg.ecosystem == "Maven":
        step = f"{where} && set {pkg.name} to {target} in the Gradle version catalog (gradle/libs.versions.toml)" + (
            ", then `./gradlew dependencies --write-locks`" if pkg.lockfile.name == "gradle.lockfile" else ""
        )
        if finding.status == "bump":
            return [step]
        return [
            step,
            f"{where} && bdt deps cooldown --exempt {pkg.name}   # Gradle has no cooldown setting: pass the exception here",
        ]
    if pkg.ecosystem == "PyPI":
        if finding.status == "bump":
            return [f"{where} && uv lock --upgrade-package {pkg.name} && uv sync"]
        return [
            f"{where} && uv lock --upgrade-package {pkg.name} --exclude-newer-package {pkg.name}=false && uv lock",
            f"{where} && uv lock --check   # and {pkg.name} must be >= {target}, with no [options.exclude-newer-package] entry",
        ]
    if finding.status == "bump":
        return [f"{where} && bun update {pkg.name}"]
    return [
        f'{where} && add "{pkg.name}@{target}" to minimumReleaseAgeExcludes in bunfig.toml (bun >= 1.4.4; older: "{pkg.name}"), '
        f"keeping existing entries, then `bun update {pkg.name}` and `git checkout bunfig.toml`",
    ]


def evaluate_package(
    pkg: LockedPackage,
    groups: list[VulnGroup],
    *,
    kev: set[str] | None,
    release_time: Callable[[Ecosystem, str, str], datetime | None],
    cooldown_days: float,
    now: datetime,
) -> Finding | None:
    if not groups:
        return None
    infos: list[GroupInfo] = []
    fixes: list[tuple[Any, str]] = []
    urgent_fixes: list[tuple[Any, str]] = []
    urgent_unknown = False
    problem: tuple[Status, str] | None = None

    for group in groups:
        in_kev = bool(kev and group.cves & kev)
        labels = group.severity_labels
        if not labels:
            severity = "unknown"
        elif len({label in URGENT_LABELS for label in labels}) > 1:
            severity = "conflicting"  # one source says high/critical, another doesn't: a human decides
        else:
            severity = max(labels, key=_SEVERITY_RANK.__getitem__)
        urgent = in_kev or (severity in URGENT_LABELS)
        states = [fix_for(v, pkg.ecosystem, pkg.name, pkg.version) for v in group.vulns]
        group_fixed = [s for s in states if s[0] == "fixed"]
        fixed_text: str | None = None
        if group.malicious:
            problem = problem or (
                "malicious",
                f"{', '.join(group.ids)}: a malicious package advisory covers the locked version, see incident.md",
            )
        elif group_fixed:
            fixed_text = max(
                (s[1] for s in group_fixed if s[1]),
                key=lambda t: parse_version(pkg.ecosystem, t),
            )
            fixes.append((parse_version(pkg.ecosystem, fixed_text), fixed_text))
            if urgent:
                urgent_fixes.append(fixes[-1])
        elif any(s[0] == "no-fix" for s in states):
            if urgent or severity in ("unknown", "conflicting"):
                problem = problem or (
                    "no-fix",
                    f"{', '.join(group.ids)}: no fixed version published",
                )
        else:
            problem = problem or (
                "needs-triage",
                f"{', '.join(group.ids)}: {states[0][2]}",
            )
        if severity in ("unknown", "conflicting") and not in_kev:
            urgent_unknown = True
        infos.append(
            GroupInfo(
                group.ids,
                sorted(group.cves),
                severity,
                in_kev,
                fixed_text,
                group.summary,
            )
        )

    finding = Finding(pkg, "informational", "", infos)
    if problem:
        finding.status, finding.reason = problem
        return finding
    if not fixes:
        finding.status, finding.reason = (
            "no-fix",
            "no affected advisory has a fixed version",
        )
        return finding

    chosen = urgent_fixes or fixes
    target = max(chosen, key=lambda f: f[0])[1]
    finding.target = target
    target_version = parse_version(pkg.ecosystem, target)
    still_open = [g for g in infos if g.fixed and parse_version(pkg.ecosystem, g.fixed) > target_version]
    open_note = (
        f"; lower-severity advisories stay open at {target} until a normal bump: "
        + ", ".join(f"{g.ids[0]} (fixed in {g.fixed})" for g in still_open)
        if urgent_fixes and still_open
        else ""
    )

    if pkg.declared:
        try:
            if not declared_range_satisfied(pkg.ecosystem, pkg.declared, target):
                finding.status = "blocked"
                finding.reason = f"the declared range {pkg.declared!r} excludes {target}: widen that specifier"
                return finding
        except VersionError as exc:
            finding.status, finding.reason = (
                "needs-triage",
                f"cannot compare the declared range with {target}: {exc}",
            )
            return finding

    released = release_time(pkg.ecosystem, pkg.name, target)
    if released is not None:
        finding.fix_age_days = round((now - released).total_seconds() / 86400, 1)
    old_enough = finding.fix_age_days is not None and finding.fix_age_days >= cooldown_days
    if old_enough:
        finding.status, finding.reason = (
            "bump",
            f"{target} is {finding.fix_age_days} days old, past the {cooldown_days:g}-day cooldown{open_note}",
        )
    elif urgent_fixes:
        age = "age unknown" if finding.fix_age_days is None else f"{finding.fix_age_days} days old"
        finding.status, finding.reason = (
            "exempt-eligible",
            f"high/critical or exploited; {target} is {age}, inside the cooldown{open_note}",
        )
    elif urgent_unknown:
        finding.status, finding.reason = (
            "needs-triage",
            "severity is missing or conflicting and the fix is inside the cooldown",
        )
    else:
        finding.status, finding.reason = (
            "informational",
            f"lower severity; {target} is inside the cooldown, wait",
        )
    finding.commands = _commands(finding)
    return finding


def scan(
    contents: Iterable[LockfileContent],
    source: AdvisorySource,
    *,
    cooldown_days: float = DEFAULT_COOLDOWN_DAYS,
    now: datetime,
) -> ScanResult:
    """Look every locked (ecosystem, name, version) up in OSV. A failing batch query raises `DataError`; a failing record
    or KEV fetch is recorded in `incomplete` and the rest is still reported."""
    result = ScanResult()
    packages: list[LockedPackage] = []
    for content in contents:
        packages.extend(content.packages)
        result.unsupported.extend(content.unsupported)
    result.scanned = len(packages)
    queries: list[PackageQuery] = [(p.ecosystem, p.name, p.version) for p in packages]
    ids_by_query = source.query_batch(queries)

    vulns: dict[str, Vuln] = {}
    for vuln_id in sorted({i for ids in ids_by_query.values() for i in ids}):
        try:
            vulns[vuln_id] = parse_vuln(source.vuln(vuln_id))
        except (DataError, KeyError, TypeError) as exc:
            result.incomplete.append(f"advisory {vuln_id} could not be read: {exc}")
    kev: set[str] | None = None
    if ids_by_query and any(ids_by_query.values()):
        try:
            kev = source.kev_cves()
        except DataError as exc:
            result.incomplete.append(f"CISA KEV unavailable, exploited-in-the-wild status unknown: {exc}")

    for pkg in packages:
        ids = ids_by_query.get((pkg.ecosystem, pkg.name, pkg.version), [])
        missing = [i for i in ids if i not in vulns]
        if missing:
            result.incomplete.append(f"{pkg.name} {pkg.version}: unreadable advisory {', '.join(missing)}")
        groups = group_vulns(vulns[i] for i in ids if i in vulns)
        try:
            finding = evaluate_package(
                pkg,
                groups,
                kev=kev,
                release_time=_safe_release_time(source, result),
                cooldown_days=cooldown_days,
                now=now,
            )
        except VersionError as exc:
            finding = Finding(pkg, "needs-triage", f"cannot evaluate: {exc}")
        if finding:
            result.findings.append(finding)
    result.findings.sort(key=lambda f: (str(f.package.lockfile), f.package.name, f.package.version))
    return result


def _safe_release_time(source: AdvisorySource, result: ScanResult) -> Callable[[Ecosystem, str, str], datetime | None]:
    def lookup(ecosystem: Ecosystem, name: str, version: str) -> datetime | None:
        try:
            return source.release_time(ecosystem, name, version)
        except DataError as exc:  # age unknown is reported as such; it never hides an advisory
            result.incomplete.append(f"release time of {name} {version} unavailable: {exc}")
            return None

    return lookup
