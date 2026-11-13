"""`bdt deps affected` / `bdt deps advisories`: read-only, they never edit a file or lock."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from .advisories import DEFAULT_COOLDOWN_DAYS, ScanResult, scan
from .cooldown import CooldownResult, changed_packages, check
from .lockfiles import LockedPackage, LockfileContent, LockfileError, Unsupported, find_lockfiles, read_lockfile
from .osv import DataError, OsvClient
from .versions import ECOSYSTEMS, VersionError, normalize_name, spec_matches

deps_app = typer.Typer(
    name="deps",
    help="Which locked dependencies (uv.lock, bun.lock, Gradle) a security advisory or supply chain incident touches",
)

PathOption = Annotated[
    list[Path] | None,
    typer.Option(
        "--path",
        help="Directory (or lockfile) to scan, repeatable; every uv.lock, bun.lock, gradle.lockfile and gradle/*.versions.toml below it. Default: the current directory.",
    ),
]
EXIT_FOUND, EXIT_SETUP, EXIT_INCOMPLETE = 1, 2, 3


def _read_all(paths: list[Path] | None) -> list[LockfileContent]:
    roots = [p for p in (paths or [Path.cwd()])]
    missing = [str(p) for p in roots if not p.exists()]
    if missing:
        typer.echo(f"bdt deps: path not found: {', '.join(missing)}", err=True)
        raise typer.Exit(EXIT_SETUP)
    try:
        lockfiles = find_lockfiles(roots)
        if not lockfiles:
            typer.echo("bdt deps: no uv.lock, bun.lock, gradle.lockfile or Gradle version catalog found", err=True)
            raise typer.Exit(EXIT_SETUP)
        return [read_lockfile(p) for p in lockfiles]
    except LockfileError as exc:
        typer.echo(f"bdt deps: {exc}", err=True)
        raise typer.Exit(EXIT_SETUP) from exc


@deps_app.command("affected")
def affected(
    package: Annotated[str, typer.Argument(help="Package name (PyPI names are normalised, npm names are exact)")],
    version_spec: Annotated[
        str | None,
        typer.Option(
            "--version-spec",
            help='Affected versions, comparators like "<4.17.21" or ">=1.0,<2" (comma = and). Default: any version.',
        ),
    ] = None,
    ecosystem: Annotated[
        str | None, typer.Option("--ecosystem", help="PyPI, npm or Maven (Gradle). Default: all.")
    ] = None,
    path: PathOption = None,
) -> None:
    """Locked versions of PACKAGE in every lockfile below --path. Exit 0 none matches, 1 a locked version matches,
    2 setup error, 3 the answer is incomplete (a git/path/alias source of that name could not be matched)."""
    if ecosystem and ecosystem not in ECOSYSTEMS:
        typer.echo(f"bdt deps: --ecosystem must be one of {', '.join(ECOSYSTEMS)}", err=True)
        raise typer.Exit(EXIT_SETUP)
    contents = _read_all(path)
    hits = 0
    incomplete = False
    for content in contents:
        if ecosystem and content.ecosystem != ecosystem:
            continue
        wanted = normalize_name(content.ecosystem, package)
        for pkg in content.packages:
            if pkg.name != wanted:
                continue
            try:
                matches = True if version_spec is None else spec_matches(pkg.ecosystem, version_spec, pkg.version)
            except VersionError as exc:
                typer.echo(f"bdt deps: {exc}", err=True)
                raise typer.Exit(EXIT_SETUP) from exc
            kind = "direct" if pkg.direct else "transitive"
            typer.echo(f"{'AFFECTED' if matches else 'ok      '} {content.lockfile}: {pkg.name} {pkg.version} ({kind})")
            hits += matches
        for item in content.unsupported:
            if wanted in (item.name, normalize_name(content.ecosystem, item.name)) or item.name.startswith(
                f"{wanted}@"
            ):
                typer.echo(f"UNKNOWN  {item.lockfile}: {item.name}: {item.reason}")
                incomplete = True
    if incomplete:
        raise typer.Exit(EXIT_INCOMPLETE)
    if not hits:
        typer.echo(f"bdt deps: no locked version of {package} matches" + (f" {version_spec}" if version_spec else ""))
    raise typer.Exit(EXIT_FOUND if hits else 0)


def _render(result: ScanResult) -> str:
    lines: list[str] = []
    for finding in result.findings:
        pkg = finding.package
        target = f" -> {finding.target}" if finding.target else ""
        age = f" (fix {finding.fix_age_days} days old)" if finding.fix_age_days is not None else ""
        lines.append(
            f"[{finding.status}] {pkg.name} {pkg.version} ({'direct' if pkg.direct else 'transitive'}){target}{age}  {pkg.lockfile}"
        )
        lines.append(f"    {finding.reason}")
        for group in finding.groups:
            flags = " ".join(filter(None, [group.severity, "KEV" if group.kev else ""]))
            lines.append(
                f"    - {' / '.join(group.ids + group.cves)} [{flags}] fixed in {group.fixed or 'n/a'}: {group.summary}"
            )
        lines.extend(f"    next: {command}" for command in finding.commands)
    for item in result.unsupported:
        lines.append(f"[unsupported] {item.name}: {item.reason}  {item.lockfile}")
    for reason in result.incomplete:
        lines.append(f"[incomplete] {reason}")
    return "\n".join(lines)


def _as_json(result: ScanResult) -> str:
    return json.dumps(dataclasses.asdict(result), indent=2, default=str)


@deps_app.command("advisories")
def advisories(
    path: PathOption = None,
    cooldown_days: Annotated[
        float, typer.Option("--cooldown-days", help="The project's dependency cooldown (see the maintenance skill).")
    ] = DEFAULT_COOLDOWN_DAYS,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """OSV advisories for every locked PyPI/npm/Maven version below --path, with a status each (malicious, needs-triage, no-fix,
    blocked, exempt-eligible, bump, informational; see the maintenance skill, audit.md). Fails closed: if OSV, the CISA KEV
    feed or a record can't be read the exit code is 3 and nothing is reported as clean. Exit 0 no affected locked version,
    1 affected, 2 setup error, 3 incomplete. Never edits anything; other lockfile sources are listed as unsupported."""
    contents = _read_all(path)
    try:
        result = scan(contents, OsvClient(), cooldown_days=cooldown_days, now=datetime.now(UTC))
    except DataError as exc:
        typer.echo(f"bdt deps: OSV could not be queried, no result: {exc}", err=True)
        raise typer.Exit(EXIT_INCOMPLETE) from exc
    typer.echo(
        _as_json(result)
        if as_json
        else _render(result) or f"bdt deps: no advisory affects the {result.scanned} locked packages"
    )
    if result.incomplete:
        raise typer.Exit(EXIT_INCOMPLETE)
    raise typer.Exit(EXIT_FOUND if result.findings else 0)


def _render_cooldown(result: CooldownResult, cooldown_days: float) -> str:
    lines = [
        f"INSIDE   {item.package.lockfile}: {item.package.name} {item.package.version} released {item.released:%Y-%m-%d %H:%M} UTC, {item.age_days} days old (cooldown {cooldown_days:g})"
        for item in result.young
    ]
    lines += [
        f"exempt   {item.package.lockfile}: {item.package.name} {item.package.version} is {item.age_days} days old"
        for item in result.exempt
    ]
    lines += [
        f"unknown  {pkg.lockfile}: {pkg.name} {pkg.version}: no public release time (private or unlisted)"
        for pkg in result.unknown
    ]
    lines += [f"[unsupported] {item.name}: {item.reason}  {item.lockfile}" for item in result.unsupported]
    lines += [f"[incomplete] {reason}" for reason in result.incomplete]
    return "\n".join(lines)


@deps_app.command("cooldown")
def cooldown(
    path: PathOption = None,
    cooldown_days: Annotated[
        float, typer.Option("--cooldown-days", help="The project's dependency cooldown (see the maintenance skill).")
    ] = DEFAULT_COOLDOWN_DAYS,
    exempt: Annotated[
        list[str] | None,
        typer.Option(
            "--exempt", help="Package (exact name, `group:artifact` for Maven) allowed inside the cooldown. Repeatable."
        ),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option(
            "--base",
            help="Only check versions that are not in the same file at this git revision (CI: origin/<target branch>). Without it every locked version is looked up.",
        ),
    ] = None,
    ecosystem: Annotated[
        str | None, typer.Option("--ecosystem", help="PyPI, npm or Maven (Gradle). Default: all.")
    ] = None,
) -> None:
    """Locked versions younger than the dependency cooldown. uv and bun enforce it while resolving; Gradle can't, so this is
    its check (`--ecosystem Maven --base origin/dev`). Exit 0 none inside the cooldown (or all exempt), 1 a version inside
    it, 2 setup error, 3 a release time could not be read. Versions without a public release time are listed, not failed."""
    if ecosystem and ecosystem not in ECOSYSTEMS:
        typer.echo(f"bdt deps: --ecosystem must be one of {', '.join(ECOSYSTEMS)}", err=True)
        raise typer.Exit(EXIT_SETUP)
    contents = [c for c in _read_all(path) if not ecosystem or c.ecosystem == ecosystem]
    packages: list[LockedPackage] = []
    unsupported: list[Unsupported] = []
    try:
        for content in contents:
            packages.extend(changed_packages(content, base) if base else content.packages)
            unsupported.extend(content.unsupported)
    except LockfileError as exc:
        typer.echo(f"bdt deps: {exc}", err=True)
        raise typer.Exit(EXIT_SETUP) from exc
    exempt_names = frozenset(exempt or ())
    result = check(
        packages, OsvClient().release_time, cooldown_days=cooldown_days, exempt=exempt_names, now=datetime.now(UTC)
    )
    result.unsupported = unsupported
    report = _render_cooldown(result, cooldown_days)
    if not result.young and not result.incomplete:
        summary = (
            f"bdt deps: none of the {result.checked} checked versions is inside the {cooldown_days:g}-day cooldown"
        )
        report = f"{report}\n{summary}" if report else summary
    typer.echo(report)
    if result.incomplete:
        raise typer.Exit(EXIT_INCOMPLETE)
    raise typer.Exit(EXIT_FOUND if result.young else 0)
