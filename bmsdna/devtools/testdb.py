"""pgdevkit test-database cleanup helpers.

The DB-naming algorithm (`workspace_db_name`, per-branch/per-suffix
expected-name computation) and the live-worktree-vs-Postgres orphan diff
used to be reimplemented here so `bdt cleanup` didn't have to import
pgdevkit. Now that pgdevkit itself exposes `find_orphaned_dbs()` and
`workspace_db_names()` (see pgdevkit's `testdb/naming.py` and
`testdb/api.py`), this module just calls into those instead of keeping a
second copy of the algorithm to drift out of sync.

What's still bdt's own concern, because pgdevkit has no notion of it:

- `db_nested_projects` -- a repo may have a wholly separate pgdevkit
  project nested in a subdirectory sharing the same branch (e.g. MDMApp's
  `akeneo_editor/`, its own `pyproject.toml`/`[tool.pgdevkit]` section).
  pgdevkit's `extra_db_suffixes` only covers a literally-suffixed sibling of
  the *same* project's main DB, not a wholly separate project name -- so
  bdt calls pgdevkit's per-project functions a second time with
  `project_root` pointed at each configured nested subdirectory instead.
  Still configured under `[tool.bdt.worktree]` in the *consuming* repo's
  pyproject.toml (as opposed to `extra_db_suffixes`, which moved to
  `[tool.pgdevkit]` in that same file -- see pgdevkit's README).

    [tool.bdt.worktree]
    db_nested_projects = ["akeneo_editor"]

- `is_caution_db` -- flagging a DB whose suffix looks like a bare branch
  name (main/test/dev/...) rather than a slugified feature branch, since
  that might be a standing reference DB rather than an orphaned leftover.
  pgdevkit's orphan detection has no opinion on this; it's purely a bdt
  cleanup-command safety heuristic layered on top of pgdevkit's results.

- Actually dropping a database by name via `psql` -- pgdevkit has no public
  "drop this arbitrary already-known db name" call (`clean_testdb()`
  resolves its own name internally, and its non-`orphaned`/`all` path
  doesn't fold in `extra_db_suffixes`), so bdt still shells out to `psql`
  itself for this, same as before.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pgdevkit.testdb as pgdevkit_testdb
from pgdevkit.testdb import constants as pgdevkit_constants
from pgdevkit.testdb.config import load_config
from pgdevkit.testdb.naming import slugify

from .bdt_config import load_bdt_table
from .cli_tools import require_psql

# A DB name ending in one of these (after stripping any configured
# `extra_db_suffixes` entry) looks like it's named after a bare branch
# rather than a slugified feature branch -- it might be a standing
# reference/baseline DB rather than an orphaned per-worktree leftover, so
# callers should flag it instead of silently treating it as safe to drop.
CAUTION_BRANCH_NAMES = {"main", "test", "dev", "head", "i18n"}


def has_pgdevkit_project(repo: Path) -> bool:
    """Whether `repo` opts into a pgdevkit-managed *Postgres* test DB: a
    `[tool.pgdevkit]` section in its pyproject.toml with no `engine =
    "mssql"`. bdt's cleanup commands only know how to list/drop databases
    via `psql`, so an mssql-engine project (or one with no pgdevkit config
    at all) is out of scope here -- unlike `pgdevkit.testdb.load_config()`,
    which always returns *some* config (falling back to the directory
    name), this distinguishes "not a pgdevkit-postgres project" from "is
    one, just using defaults"."""
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    section = data.get("tool", {}).get("pgdevkit")
    # `is None` on purpose, not `not section` -- an empty `[tool.pgdevkit]`
    # (accepting every default) parses to `{}`, which is falsy but very much
    # still opted in.
    if section is None:
        return False
    return section.get("engine", "postgres") == "postgres"


def db_nested_projects(repo: Path) -> list[str]:
    """`[tool.bdt.worktree].db_nested_projects` -- subdirectory names that
    are wholly separate pgdevkit projects (their own `[tool.pgdevkit]`
    section) sharing this worktree's branch."""
    raw = load_bdt_table("worktree", repo).get("db_nested_projects", [])
    return [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []


def project_roots(repo: Path) -> list[Path]:
    """`repo` itself (if it's a pgdevkit-postgres project), plus every
    configured `db_nested_projects` subdirectory that exists.

    Unlike `repo` itself, a configured nested project is *not* also gated
    on `has_pgdevkit_project()` -- being named in `db_nested_projects` at
    all is the opt-in. pgdevkit's own `load_config()` falls back to the
    directory name whenever a `[tool.pgdevkit]` section is missing (as long
    as *some* pyproject.toml exists there), and real nested projects rely
    on exactly that: MDMApp's actual `akeneo_editor/pyproject.toml` has no
    `[tool.pgdevkit]` section at all, yet its own tests call
    `pgdevkit.testdb.ensure_testdb()` directly and it works, resolving to
    project name "akeneo_editor" via the directory-name fallback. Requiring
    an explicit section here too would silently exclude it."""
    roots = [repo] if has_pgdevkit_project(repo) else []
    roots += [nested_root for nested in db_nested_projects(repo) if (nested_root := repo / nested).is_dir()]
    return roots


def project_name(repo: Path) -> str | None:
    """This repo's pgdevkit project name, or None if it isn't a
    pgdevkit-managed Postgres project at all."""
    if not has_pgdevkit_project(repo):
        return None
    return load_config(repo).name


def workspace_db_names(repo: Path) -> frozenset[str]:
    """Every DB name this exact worktree at `repo` owns right now --
    `repo`'s own main DB plus any configured `extra_db_suffixes` siblings
    (both resolved by pgdevkit), plus the same for any configured
    `db_nested_projects`. Requires `repo` to still be a valid git checkout
    (pgdevkit resolves the current branch from it), so this must be called
    before the worktree is removed."""
    names: set[str] = set()
    for root in project_roots(repo):
        names |= pgdevkit_testdb.workspace_db_names(project_root=root)
    return frozenset(names)


def find_orphaned(repo: Path) -> list[tuple[str, str, bool]]:
    """(db_name, project_name, caution) for every database with no matching
    live git worktree, across this repo's own pgdevkit project and any
    configured nested projects -- delegates the actual diffing entirely to
    pgdevkit's own `find_orphaned_dbs()`, once per project root; `caution`
    is bdt's own heuristic on top (see `is_caution_db`)."""
    results: list[tuple[str, str, bool]] = []
    for root in project_roots(repo):
        config = load_config(root)
        suffixes = list(config.extra_db_suffixes)
        for name in pgdevkit_testdb.find_orphaned_dbs(project_root=root):
            results.append((name, config.name, is_caution_db(name, config.name, suffixes)))
    return results


def is_caution_db(db_name: str, project_name: str, sibling_suffixes: list[str]) -> bool:
    """Flag DBs whose suffix is a bare branch name (main/test/dev/...)
    rather than a slugified feature branch -- these may be standing
    reference DBs, not per-worktree leftovers.

    `db_name` is always pgdevkit's *slugified* name (lowercased, invalid
    chars collapsed), so `project_name` must be slugified the same way
    before comparing -- otherwise a project name with uppercase/special
    characters (e.g. "MDMApp") never matches its own DBs (e.g.
    "mdmapp_main"), and this silently never fires."""
    prefix = f"{slugify(project_name)}_"
    if not db_name.startswith(prefix):
        return False
    tail = db_name[len(prefix) :]
    for suffix in sibling_suffixes:
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
    return tail in CAUTION_BRANCH_NAMES


def _run_psql(args: list[str], pg_host: str, pg_port: int, pg_user: str) -> subprocess.CompletedProcess:
    """Run psql against the given host/port/user, authenticating with
    pgdevkit's own test-container password over TCP -- matching exactly how
    pgdevkit's own find_orphaned_dbs()/workspace_db_names() connect, so the
    listing half and this DROP step can't end up silently targeting two
    different Postgres instances (no -h means psql defaults to the unix
    socket, which is peer- not password-authenticated)."""
    psql = require_psql()
    env = {**os.environ, "PGPASSWORD": pgdevkit_constants.PASSWORD}
    return subprocess.run(
        [psql, "-h", pg_host, "-p", str(pg_port), "-U", pg_user, "-d", "postgres", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def drop_database(name: str, pg_host: str, pg_port: int, pg_user: str) -> subprocess.CompletedProcess:
    # Escape embedded `"` by doubling it, per Postgres quoted-identifier
    # rules -- `name` comes from a live `pg_database` listing (via
    # pgdevkit), not a bdt-validated slug, so it isn't guaranteed to already
    # be injection-safe.
    escaped = name.replace('"', '""')
    return _run_psql(["-c", f'DROP DATABASE IF EXISTS "{escaped}"'], pg_host, pg_port, pg_user)
