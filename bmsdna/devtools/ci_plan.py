"""Which tests does this CI run execute? Pure decision logic behind `bdt ci plan` (I/O lives in `ci_plan_env`).

Configured under `[tool.bdt.ci.plan]` in pyproject.toml (the table must exist, even if empty; everything is optional):

    [tool.bdt.ci.plan]
    on_missing = "infer"            # PR without a usable label: "infer" from the changed files (default) or "fail"
    non_code = ["docs/**", "*.md"]  # files no test reads or ships; explicit per repo (`*.md` = root only, `**/*.md` = all)

    [tool.bdt.ci.plan.tiers.unit]   # default tiers: unit = tests/unit, e2e = tests/e2e
    dir = "tests/unit"              # areas = sub-folders with tests (snake_case folder <-> kebab-case label)

    [tool.bdt.ci.plan.tiers.e2e]
    dir = "tests/e2e"               # flat folder: an area table maps test files to areas instead
    areas = { chat = ["test_chat*.py"], review = ["test_review_*.py"] }  # unmatched files = area "shared"

Labels: `test-<area>`, `test-full` (everything), `test-none` (no tests; lint and build still run). The legacy
`e2e-<area>` labels count as `test-<area>`. Order of decision (the reason is always reported):

1. Not a PR: the CI run after a push (merge) to `test`/`main` runs nothing (promoted code was tested on its way
   into `dev`); anything else (nightly, manual runs, other branches) and forks run everything.
2. Branch flow: promotions (dev -> test -> main -> dev) run nothing; `fix/*` and `hotfix/*` into `main` are tested
   and must be cut from `main`; any other PR into `test`/`main` is an error.
3. PR labels decide. Without a usable label the changed files decide (`on_missing = "infer"`): nothing changed or
   only `non_code` files -> none, only area tests -> those areas, any other file -> everything.
4. Anything unreadable (label API, change list) selects everything.
"""

import shlex
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from .bdt_config import find_pyproject, load_bdt_table

Kind = Literal["full", "none", "areas", "error"]
OnMissing = Literal["infer", "fail"]

LABEL_PREFIX = "test-"
LEGACY_LABEL_PREFIX = "e2e-"
FULL_LABEL = "test-full"
NONE_LABEL = "test-none"
DEFAULT_TIER_DIRS = {"unit": "tests/unit", "e2e": "tests/e2e"}
TEST_FILE_GLOB = "test_*.py"
UNMATCHED_AREA = "shared"
LONG_LIVED = frozenset({"dev", "test", "main"})
PROMOTIONS = frozenset({("dev", "test"), ("test", "main"), ("main", "dev")})
FIX_PREFIXES = ("fix/", "hotfix/")
PUSH_REASONS = frozenset(
    {"IndividualCI", "BatchedCI"}
)  # a merge landed on the branch: deploy only
RESERVED_AREAS = frozenset(
    {"full", "none"}
)  # test-full / test-none are the control labels


class BranchFlowError(Exception):
    """A fix branch for `main` that was not cut from `main`."""


def _is_test_file(name: str) -> bool:
    return any(PurePosixPath(name).match(g) for g in (TEST_FILE_GLOB, "*_test.py"))


def _has_tests(folder: Path) -> bool:
    return any(
        p.is_file()
        and _is_test_file(p.name)
        and "__pycache__" not in p.relative_to(folder).parts
        for p in folder.rglob("*.py")
    )


@dataclass(frozen=True)
class Tier:
    """One test tier (unit, e2e, ...). Areas are the sub-folders of `dir` that contain tests (`test_*.py` or
    `*_test.py`; output and helper folders are not areas), or, with a `globs` table, groups of the `test_*.py` files
    directly in `dir`."""

    name: str
    dir: str
    globs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def _files(self, root: Path) -> list[str]:
        base = root / self.dir
        return sorted(
            p.relative_to(root).as_posix()
            for p in base.glob(TEST_FILE_GLOB)
            if p.is_file()
        )

    def _owners(self, path: str) -> frozenset[str]:
        name = PurePosixPath(path).name
        owned = {
            area
            for area, globs in self.globs.items()
            if any(PurePosixPath(name).full_match(g) for g in globs)
        }
        return frozenset(owned or {UNMATCHED_AREA})

    def areas(self, root: Path) -> list[str]:
        base = root / self.dir
        if not base.is_dir():
            return []
        if self.globs:
            return sorted(
                {a for f in self._files(root) for a in self._owners(f)}
                | set(self.globs)
            )
        return sorted(
            d.name
            for d in base.iterdir()
            if d.is_dir() and not d.name.startswith(("_", ".")) and _has_tests(d)
        )

    def area_of(
        self, root: Path, path: str, areas: Collection[str]
    ) -> frozenset[str] | None:
        """Areas owning a repo-relative file, or None when the file is not an area file of this tier (a helper
        folder, a file directly in the tier folder): changing it can affect any test, so the plan runs everything.
        In folder mode a file of an area folder counts, helpers like conftest.py included. A test file whose
        whole folder was removed still names its (now gone) area. `areas` is `self.areas(root)`, computed once per
        plan by the caller."""
        pure = PurePosixPath(path)
        if not pure.is_relative_to(self.dir):
            return None
        parts = pure.relative_to(self.dir).parts
        if self.globs:
            return (
                self._owners(path)
                if len(parts) == 1 and pure.match(TEST_FILE_GLOB)
                else None
            )
        if len(parts) < 2 or parts[0].startswith(("_", ".")):
            return None
        removed = not (root / self.dir / parts[0]).exists() and _is_test_file(pure.name)
        return frozenset({parts[0]}) if parts[0] in areas or removed else None

    def paths(self, root: Path, selected: Collection[str] | None) -> list[str]:
        """What the test runner gets: the selected areas' folders or files, or (None) the whole tier."""
        if not (root / self.dir).is_dir():
            return []
        if self.globs:
            return [
                f
                for f in self._files(root)
                if selected is None or self._owners(f) & set(selected)
            ]
        if selected is None:
            return [self.dir]
        return [f"{self.dir}/{a}" for a in self.areas(root) if a in selected]


@dataclass(frozen=True)
class PlanConfig:
    root: Path
    tiers: tuple[Tier, ...]
    on_missing: OnMissing = "infer"
    non_code: tuple[str, ...] = ()

    def all_areas(self) -> set[str]:
        return {a for tier in self.tiers for a in tier.areas(self.root)}

    def is_non_code(self, path: str) -> bool:
        return any(PurePosixPath(path).full_match(g) for g in self.non_code)


def _strings(value: object, where: str) -> tuple[str, ...]:
    items = (
        tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()
    )
    if not isinstance(value, list) or len(items) != len(value):
        raise ValueError(f"[tool.bdt.ci.plan] {where} must be a list of strings")
    return items


def _tier(name: str, raw: object) -> Tier:
    table = raw if isinstance(raw, dict) else {}
    directory = table.get("dir", DEFAULT_TIER_DIRS.get(name))
    areas = table.get("areas", {})
    if not isinstance(directory, str):
        raise ValueError(f"[tool.bdt.ci.plan.tiers.{name}] needs a string `dir`")
    if not isinstance(areas, dict):
        raise ValueError(
            f"[tool.bdt.ci.plan.tiers.{name}] areas must be a table of glob lists"
        )
    globs = {
        str(area): _strings(g, f"tiers.{name}.areas.{area}")
        for area, g in areas.items()
    }
    return Tier(name, directory.strip("/"), globs)


def load_plan_config(start: Path | None = None) -> PlanConfig | None:
    """`[tool.bdt.ci.plan]`, or None when the table is absent (the feature is opt-in per repo)."""
    raw = load_bdt_table("ci", start).get("plan")
    pyproject = find_pyproject(start)
    if not isinstance(raw, dict) or pyproject is None:
        return None
    on_missing = raw.get("on_missing", "infer")
    if on_missing not in ("infer", "fail"):
        raise ValueError('[tool.bdt.ci.plan] on_missing must be "infer" or "fail"')
    tiers_raw = raw.get("tiers", {name: {} for name in DEFAULT_TIER_DIRS})
    if not isinstance(tiers_raw, dict):
        raise ValueError("[tool.bdt.ci.plan.tiers] must be a table")
    config = PlanConfig(
        root=pyproject.parent,
        tiers=tuple(_tier(name, t) for name, t in tiers_raw.items()),
        on_missing=on_missing,
        non_code=_strings(raw.get("non_code", []), "non_code"),
    )
    if clash := sorted(config.all_areas() & RESERVED_AREAS):
        raise ValueError(
            f"[tool.bdt.ci.plan] the area name(s) {', '.join(clash)} collide with the test-full / test-none labels; rename them"
        )
    return config


def area_label(area: str) -> str:
    return LABEL_PREFIX + area.replace("_", "-")


def required_label_choices(config: PlanConfig) -> list[str]:
    """Every label `bdt pr create` accepts as the PR's test decision (legacy `e2e-*` spellings included)."""
    labels = [
        *(area_label(a) for a in sorted(config.all_areas())),
        FULL_LABEL,
        NONE_LABEL,
    ]
    return [
        *labels,
        *(LEGACY_LABEL_PREFIX + label.removeprefix(LABEL_PREFIX) for label in labels),
    ]


def normalize_labels(labels: Iterable[str]) -> set[str]:
    """Lowercase (`bdt pr create` matches case-insensitively) and map legacy `e2e-*` to `test-*`."""
    lowered = {label.lower() for label in labels}
    return {
        LABEL_PREFIX + x.removeprefix(LEGACY_LABEL_PREFIX)
        if x.startswith(LEGACY_LABEL_PREFIX)
        else x
        for x in lowered
    }


@dataclass(frozen=True)
class Context:
    """What is being built: a PR (`pr_id` set) or a plain branch build."""

    branch: str = ""
    reason: str = ""  # Azure's Build.Reason for branch builds (IndividualCI, BatchedCI, Schedule, Manual, ...)
    pr_id: int | None = None
    source: str = ""
    target: str = ""
    is_fork: bool = False


@dataclass(frozen=True)
class Plan:
    kind: Kind
    reason: str
    areas: frozenset[str] = frozenset()
    warnings: tuple[str, ...] = ()


def classify_pr(
    source: str, target: str
) -> tuple[Literal["promotion", "tested", "rejected"], str]:
    """How a PR from `source` into `target` (branch names, no `refs/heads/`) is treated."""
    if source in LONG_LIVED and target in LONG_LIVED:
        if (source, target) in PROMOTIONS:
            return "promotion", f"{source} -> {target}"
        return (
            "rejected",
            f"{source} -> {target} is not a promotion (order is dev -> test -> main, then main -> dev)",
        )
    if target == "test":
        return (
            "rejected",
            f"only the dev -> test promotion may target test, not {source}",
        )
    if target == "main":
        if source.startswith(FIX_PREFIXES):
            return "tested", f"fix branch {source} -> main"
        return (
            "rejected",
            f"only fix/*, hotfix/* branches and the test -> main promotion may target main, not {source}",
        )
    return "tested", f"{source} -> {target}"


def _full(config: PlanConfig, reason: str, warnings: Iterable[str] = ()) -> Plan:
    return Plan("full", reason, frozenset(config.all_areas()), tuple(warnings))


def _from_labels(
    labels: Iterable[str], config: PlanConfig, warnings: list[str]
) -> Plan | None:
    normalized = normalize_labels(labels)
    known = config.all_areas()
    if FULL_LABEL in normalized:
        return _full(config, f"PR has the {FULL_LABEL} label", warnings)
    areas = frozenset(a for a in known if area_label(a) in normalized)
    unknown = sorted(
        x
        for x in normalized
        if x.startswith(LABEL_PREFIX)
        and x not in {NONE_LABEL} | {area_label(a) for a in known}
    )
    if unknown:
        warnings.append(f"unknown test label(s) ignored: {', '.join(unknown)}")
    if NONE_LABEL in normalized and areas:
        warnings.append(f"{NONE_LABEL} is ignored because area labels are present")
    if areas:
        return Plan(
            "areas",
            "PR labels select " + ", ".join(sorted(areas)),
            areas,
            tuple(warnings),
        )
    if NONE_LABEL in normalized:
        return Plan("none", f"PR has the {NONE_LABEL} label", warnings=tuple(warnings))
    return None


def _from_files(files: list[str], config: PlanConfig, warnings: list[str]) -> Plan:
    if not files:
        return Plan("none", "no changed files (a sync merge)", warnings=tuple(warnings))
    known = {tier.name: frozenset(tier.areas(config.root)) for tier in config.tiers}
    areas: set[str] = set()
    for path in files:
        if config.is_non_code(path):
            continue
        owners = [
            owned
            for tier in config.tiers
            if (owned := tier.area_of(config.root, path, known[tier.name]))
        ]
        if not owners:
            return _full(
                config,
                f"{path} is not owned by an area (shared code, config or an unmapped test)",
                warnings,
            )
        areas.update(a for owned in owners for a in owned)
    if not areas:
        return Plan(
            "none",
            f"all {len(files)} changed file(s) are non-code",
            warnings=tuple(warnings),
        )
    areas &= frozenset().union(
        *known.values()
    )  # a removed area has nothing left to run
    if not areas:
        return Plan(
            "none",
            "the changed area tests no longer exist (removed)",
            warnings=tuple(warnings),
        )
    return Plan(
        "areas",
        f"changed area tests: {', '.join(sorted(areas))}",
        frozenset(areas),
        tuple(warnings),
    )


def make_plan(
    ctx: Context,
    config: PlanConfig,
    *,
    labels: Callable[[], list[str]],
    changed_files: Callable[[], list[str]],
    verify_fix_base: Callable[[], None],
) -> Plan:
    """The decision. The three callables are the only I/O: PR labels, changed files, and the fix-branch base check
    (raises BranchFlowError on a violation). Unreadable inputs (OSError, ValueError, RuntimeError) select everything."""
    if ctx.pr_id is None:
        if ctx.branch in {"test", "main"} and ctx.reason in PUSH_REASONS:
            return Plan(
                "none",
                f"push to {ctx.branch}: that code was tested on its way into dev, deploy only",
            )
        return _full(config, "not a PR build")
    if ctx.is_fork:
        return _full(config, "PR from a fork")
    kind, why = classify_pr(ctx.source, ctx.target)
    if kind == "rejected":
        return Plan("error", f"branch flow violated: {why}")
    if kind == "promotion":
        return Plan("none", f"promotion {why}: already tested, no tests run")
    warnings: list[str] = []
    if ctx.target == "main":
        try:
            verify_fix_base()
        except BranchFlowError as error:
            return Plan("error", f"branch flow violated: {error}")
        except (OSError, ValueError, RuntimeError) as error:
            warnings.append(f"fix-branch base not verified: {error}")
    try:
        found = _from_labels(labels(), config, warnings)
    except (OSError, ValueError, RuntimeError) as error:
        return _full(
            config,
            f"PR labels could not be read ({error}), running everything",
            warnings,
        )
    if found:
        return found
    if config.on_missing == "fail":
        known = ", ".join(area_label(a) for a in sorted(config.all_areas()))
        return Plan(
            "error",
            f"PR has no test label: add one of {known}, {FULL_LABEL} or {NONE_LABEL}",
        )
    try:
        files = changed_files()
    except (OSError, ValueError, RuntimeError) as error:
        return _full(
            config,
            f"no test label and the changed files could not be listed ({error})",
            warnings,
        )
    warnings.append(
        f"PR has no test label: areas inferred from the changed files (add a {LABEL_PREFIX}<area> label)"
    )
    return _from_files(files, config, warnings)


def outputs(plan: Plan, config: PlanConfig) -> dict[str, str]:
    """Pipeline variables: `runTests`, then per tier `<tier>Run`, `<tier>Areas` and `<tier>Paths` (a shell-quoted
    argument list for the test runner)."""
    run = plan.kind in {"full", "areas"}
    selected = None if plan.kind == "full" else plan.areas
    result = {"runTests": str(run).lower()}
    for tier in config.tiers:
        paths = tier.paths(config.root, selected) if run else []
        areas = (
            [a for a in tier.areas(config.root) if selected is None or a in selected]
            if run
            else []
        )
        result[f"{tier.name}Run"] = str(bool(paths)).lower()
        result[f"{tier.name}Areas"] = " ".join(areas)
        result[f"{tier.name}Paths"] = shlex.join(paths)
    return result
