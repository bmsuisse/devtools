"""pgdevkit test-database naming and cleanup helpers.

pgdevkit (github.com/bmsuisse/pgdevkit) names each git worktree's local
Postgres test database `workspace_db_name(project_name, branch)` --
`slugify`/`workspace_db_name` below reproduce that algorithm exactly (see
pgdevkit's `testdb/naming.py`) so `bdt cleanup` can compute, without ever
importing pgdevkit itself, which database belongs to which worktree.

A few repos layer extra sibling/nested test DBs on top of the one pgdevkit
creates for the main project (e.g. a second DB for a vendored mock service,
or a wholly separate DB for a nested sub-project on the same branch).
pgdevkit has no notion of this, so it's configured per-repo, under
`[tool.bdt.worktree]` in that repo's own pyproject.toml, rather than baked
into this shared package as knowledge of specific downstream repos:

    [tool.bdt.worktree]
    # "<main_db>_onetrade" is also created alongside the main workspace DB.
    db_sibling_suffixes = ["_onetrade"]
    # A wholly separate per-branch DB, named as if "akeneo_editor" were its
    # own pgdevkit project, also belongs to this worktree.
    db_nested_projects = ["akeneo_editor"]
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from .bdt_config import load_bdt_table
from .cli_tools import PSQL_INSTALL_HINT

_INVALID_CHARS = re.compile(r"[^a-z0-9_]+")
_MAX_SLUG_LEN = 30

# A DB name ending in one of these (after stripping any configured sibling
# suffix) looks like it's named after a bare branch rather than a slugified
# feature branch -- it might be a standing reference/baseline DB rather than
# an orphaned per-worktree leftover, so callers should flag it instead of
# silently treating it as safe to drop.
CAUTION_BRANCH_NAMES = {"main", "test", "dev", "head", "i18n"}


def slugify(value: str) -> str:
    """Mirrors pgdevkit.testdb.naming.slugify exactly: lowercase, collapse
    runs of non `[a-z0-9_]` chars to a single `_`, strip leading/trailing
    `_`, and hash-truncate anything over 30 chars so long branch/project
    names can't collide with Postgres' own identifier length limit."""
    slug = _INVALID_CHARS.sub("_", value.lower()).strip("_")
    if not slug:
        slug = "x"
    if len(slug) <= _MAX_SLUG_LEN:
        return slug
    digest = hashlib.sha256(slug.encode()).hexdigest()[:8]
    return f"{slug[:_MAX_SLUG_LEN]}_{digest}"


def workspace_db_name(project_name: str, branch: str) -> str:
    """Mirrors pgdevkit.testdb.naming.workspace_db_name exactly."""
    joined = f"{slugify(project_name)}_{slugify(branch)}"
    return slugify(joined)


def read_pgdevkit_project(repo: Path) -> str | None:
    """`[tool.pgdevkit].name` from repo's root pyproject.toml, or None if the
    repo has no pgdevkit-managed Postgres test DB (no section, or a
    non-postgres engine, e.g. mssql)."""
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    section = data.get("tool", {}).get("pgdevkit")
    if not section:
        return None
    if section.get("engine", "postgres") != "postgres":
        return None
    return section.get("name") or repo.name


def db_sibling_suffixes(repo: Path) -> list[str]:
    """`[tool.bdt.worktree].db_sibling_suffixes` -- extra DBs named
    "<main_db><suffix>" that belong alongside a worktree's main test DB."""
    raw = load_bdt_table("worktree", repo).get("db_sibling_suffixes", [])
    return [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []


def db_nested_projects(repo: Path) -> list[str]:
    """`[tool.bdt.worktree].db_nested_projects` -- other pgdevkit project
    names (as `workspace_db_name` would compute them) that share this
    worktree's branch but are otherwise unrelated projects."""
    raw = load_bdt_table("worktree", repo).get("db_nested_projects", [])
    return [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []


def expected_db_names(repo: Path, project_name: str, branch: str) -> frozenset[str]:
    """All DB names a given branch is expected to own for `project_name`:
    the main workspace DB, any configured sibling DBs, and any
    nested-project DBs sharing the same branch."""
    main_db = workspace_db_name(project_name, branch)
    names = {main_db}
    for suffix in db_sibling_suffixes(repo):
        names.add(f"{main_db}{suffix}")
    for nested in db_nested_projects(repo):
        names.add(workspace_db_name(nested, branch))
    return frozenset(names)


def is_caution_db(db_name: str, project_name: str, sibling_suffixes: list[str]) -> bool:
    """Flag DBs whose suffix is a bare branch name (main/test/dev/...)
    rather than a slugified feature branch -- these may be standing
    reference DBs, not per-worktree leftovers."""
    prefix = f"{project_name}_"
    if not db_name.startswith(prefix):
        return False
    tail = db_name[len(prefix) :]
    for suffix in sibling_suffixes:
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
    return tail in CAUTION_BRANCH_NAMES


def _run_psql(args: list[str], pg_port: int, pg_user: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["psql", "-p", str(pg_port), "-U", pg_user, "-d", "postgres", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        sys.exit(f"'psql' is required for this command but wasn't found on PATH.\n{PSQL_INSTALL_HINT}")


def list_databases(pg_port: int, pg_user: str) -> list[str]:
    result = _run_psql(
        ["-Atc", "select datname from pg_database where datistemplate = false order by datname"],
        pg_port,
        pg_user,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def drop_database(name: str, pg_port: int, pg_user: str) -> subprocess.CompletedProcess:
    return _run_psql(["-c", f'DROP DATABASE IF EXISTS "{name}"'], pg_port, pg_user)
