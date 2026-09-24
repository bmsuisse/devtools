"""Create a git worktree for a new branch, mirroring the `just worktree`
recipes -- and, separately, `bdt cleanup worktrees` / `bdt cleanup
orphaned-dbs` / `bdt cleanup db` / `bdt cleanup worktree`: pruning worktrees
already merged into main/test (and their pgdevkit-managed Postgres test
DB(s), see testdb.py), sweeping for test DBs whose worktree is already gone
some other way, dropping a single still-live worktree's own test DB(s) on
demand, and tearing down one specific still-live worktree (regardless of
merge status) plus its DB(s) in one go.

Every path here that drops a database also runs it past
`testdb.confirm_remote_host()` first -- an unconditional, un-overridable
interactive check for any non-local `--pg-host` (see that function's
docstring).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import pull as pull_mod
from . import testdb

# Heavy/vendor/build directories that are never themselves a separate repo
# worth discovering when scanning for repos under a root. Dotdirs (.venv,
# .git, .cache, .tox, .idea, ...) are skipped separately, so this only needs
# non-dot names.
_SKIP_DIRS = {
    "node_modules",
    "venv",
    "dist",
    "build",
    "out",
    "target",
    "vendor",
    "bower_components",
    "coverage",
    "bin",
    "obj",
}

# A worktree on one of these branches is never offered for removal, merged
# or not -- they're the long-lived branches everything else merges into.
PROTECTED_BRANCHES = {"main", "test"}


def _run(cmd: list[str], cwd: Path) -> None:
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
    except FileNotFoundError:
        sys.exit(f"'{cmd[0]}' is required for this command but wasn't found on PATH.")


def _run_capture(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def create(
    name: str,
    base: str = "dev",
    env_file: str | None = None,
    submodules: bool = True,
    install_cmd: list[str] | None = None,
    root: Path | None = None,
) -> Path:
    root = root or Path.cwd()
    path = root / ".worktrees" / name
    if path.exists():
        sys.exit(f"error: {path} already exists")

    _run(["git", "worktree", "add", str(path), "-b", name, base], cwd=root)

    if submodules and (root / ".gitmodules").exists():
        _run(["git", "submodule", "update", "--init"], cwd=path)

    if env_file is None:
        for candidate in (".local_env", ".env"):
            if (root / candidate).exists():
                env_file = candidate
                break

    if env_file and (root / env_file).exists():
        shutil.copy(root / env_file, path / ".env")

    if install_cmd:
        _run(install_cmd, cwd=path)

    print(f"worktree ready at {path}")
    # A worktree based on the remote's actual DEFAULT branch makes `bdt pull`'s
    # separate default-branch step redundant -- checked against the real thing
    # (via `git ls-remote`, same as `pull.default_branch`) rather than just
    # assuming main/master IS the default, which isn't always true (e.g. a
    # repo defaulting to `develop`). Falls back to that main/master assumption
    # only if the remote can't be reached from here.
    remote_default = pull_mod.default_branch("origin", cwd=path)
    originated_from_default = base == remote_default if remote_default is not None else base in ("main", "master")
    if originated_from_default:
        print(f"Hint: run `bdt pull --no-default` in it to pull the latest {base} (it's also the default branch, so pulling that again would be redundant).")
    else:
        print("Hint: run `bdt pull` in it to pull the latest main/master and default branch.")
    return path


# --- Discovery: repos and their worktrees -------------------------------


def find_repos(root: Path) -> list[Path]:
    """Recursively find repo roots (dirs with a `.git` directory) under root.

    Stops descending as soon as it hits a `.git` (whether a real repo or a
    linked worktree/submodule's `.git` file) and skips node_modules-style
    build/vendor dirs and all dotdirs, so it never wastes time walking into
    the huge trees those tend to contain.
    """
    repos: list[Path] = []
    stack = [str(root)]
    while stack:
        current = stack.pop()
        git_path = os.path.join(current, ".git")
        if os.path.isdir(git_path):
            repos.append(Path(current))
            continue
        if os.path.exists(git_path):
            # A `.git` file means this is a linked worktree or submodule
            # checkout; its worktrees are already reachable from the repo
            # it belongs to, so there's nothing new to find by descending.
            continue
        try:
            with os.scandir(current) as it:
                subdirs = [
                    entry.path
                    for entry in it
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".") and entry.name not in _SKIP_DIRS
                ]
        except (PermissionError, NotADirectoryError, FileNotFoundError):
            continue
        stack.extend(subdirs)
    return repos


def _main_repo_root(worktree_path: Path) -> Path | None:
    """Resolve the main checkout that owns the (possibly linked) worktree at
    `worktree_path` -- its parent directory of `--git-common-dir`, which
    always points at the main repo's `.git`, regardless of which worktree
    it's queried from. `git worktree remove`/`list` need to run with a cwd
    inside *some* still-existing worktree of the repo; using the main
    checkout rather than `worktree_path` itself keeps that true even after
    `worktree_path` is deleted mid-call."""
    result = _run_capture(["git", "-C", str(worktree_path), "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).parent


def _parse_worktree_list(repo: Path) -> list[dict]:
    result = _run_capture(["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return []
    entries: list[dict] = []
    current: dict = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"path": line[len("worktree ") :].strip()}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "bare":
            current["bare"] = True
        elif line.startswith("locked"):
            current["locked"] = True
    if current:
        entries.append(current)
    return entries


def _ref_exists(repo: Path, ref: str) -> bool:
    result = _run_capture(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref])
    return result.returncode == 0


def _is_ancestor(repo: Path, commit: str, ref: str) -> bool:
    result = _run_capture(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, ref])
    return result.returncode == 0


def _is_dirty(worktree_path: Path) -> bool:
    # No --ignore-submodules here: a bare `--ignore-submodules` flag means
    # `--ignore-submodules=all` to git, which would hide real uncommitted
    # changes inside a submodule and let a genuinely dirty worktree look
    # clean. The default (equivalent to --ignore-submodules=none for our
    # purposes) reports those correctly.
    result = _run_capture(["git", "-C", str(worktree_path), "status", "--porcelain"])
    return bool(result.stdout.strip())


@dataclass
class Worktree:
    repo: Path
    path: Path
    head: str
    branch: str | None
    is_main: bool
    locked: bool
    dirty: bool = False
    merged_into: list[str] = field(default_factory=list)
    db_names: frozenset[str] = frozenset()

    @property
    def removable(self) -> bool:
        if self.is_main or self.locked or self.dirty:
            return False
        if self.branch in PROTECTED_BRANCHES:
            return False
        return bool(self.merged_into)


def collect_worktrees(repo: Path, remote: str) -> list[Worktree]:
    """List every worktree of `repo`, each annotated with which of
    `<remote>/main`, `<remote>/test` (falling back to local `main`/`test` if
    no such remote refs exist) its HEAD is merged into, and which pgdevkit
    test DB name(s) it's expected to own."""
    entries = _parse_worktree_list(repo)
    if not entries:
        return []

    candidate_refs = [f"{remote}/main", f"{remote}/test", "main", "test"]
    existing_refs = [r for r in candidate_refs if _ref_exists(repo, r)]

    worktrees: list[Worktree] = []
    for i, e in enumerate(entries):
        if e.get("bare"):
            continue
        path = Path(e["path"])
        head = e.get("head", "")
        branch = e.get("branch")
        # testdb.workspace_db_names() resolves the project name and current
        # branch itself from `path`'s own pyproject.toml/git checkout, so
        # this needs `path` to still exist -- fine here since a worktree
        # only gets removed (and its DB(s) dropped) later in this same
        # collect-then-remove flow, well after this runs.
        db_names = testdb.workspace_db_names(path) if path.exists() else frozenset()
        wt = Worktree(
            repo=repo,
            path=path,
            head=head,
            branch=branch,
            is_main=(i == 0),
            locked=bool(e.get("locked")),
            db_names=db_names,
        )
        if not wt.is_main and path.exists():
            wt.dirty = _is_dirty(path)
        if head:
            for ref in existing_refs:
                if _is_ancestor(repo, head, ref):
                    wt.merged_into.append(ref)
        worktrees.append(wt)
    return worktrees


# --- `bdt cleanup worktrees` ---------------------------------------------


def remove_worktree(wt: Worktree, *, drop_dbs: bool, pg_host: str, pg_port: int, pg_user: str) -> tuple[subprocess.CompletedProcess, list[tuple[str, bool]]]:
    """Remove a worktree, retrying with --force if git blocks it solely
    because the worktree contains submodules.

    Git unconditionally refuses `worktree remove` on any worktree that has
    submodules ("fatal: working trees containing submodules cannot be moved
    or removed"), even when it's perfectly clean. Since callers only reach
    here for a worktree that's already been verified as clean (submodules
    included, see _is_dirty) and merged, forcing past that specific refusal
    is safe.

    Returns the git result plus a (db_name, dropped_ok) list for any
    associated Postgres test DBs dropped along with it.
    """
    result = _run_capture(["git", "-C", str(wt.repo), "worktree", "remove", str(wt.path)])
    if result.returncode != 0 and "submodules cannot be moved or removed" in result.stderr:
        result = _run_capture(["git", "-C", str(wt.repo), "worktree", "remove", "--force", str(wt.path)])
    db_results: list[tuple[str, bool]] = []
    if drop_dbs and result.returncode == 0:
        for db in sorted(wt.db_names):
            dr = testdb.drop_database(db, pg_host, pg_port, pg_user)
            db_results.append((db, dr.returncode == 0))
    return result, db_results


def clean_worktrees(root: Path, *, remote: str, keep_dbs: bool, yes: bool, pg_host: str, pg_port: int, pg_user: str) -> None:
    """Find every worktree merged into `<remote>/main`/`<remote>/test` (or
    local main/test) across every repo found under `root`, and remove them
    (and, unless `keep_dbs`, their pgdevkit test DB(s)) -- but only when
    `yes` is set. Without it, this just prints what would be removed, same
    as `bdt issue delete` requiring an explicit `--yes` rather than an
    interactive prompt (bdt is also invoked by AI-agent callers)."""
    root = root.resolve()
    repos = find_repos(root)
    if not repos:
        print(f"No git repositories found under {root}.")
        return

    by_repo = {repo: collect_worktrees(repo, remote) for repo in sorted(repos)}

    candidates: list[Worktree] = []
    skipped_notes: list[str] = []
    for worktrees in by_repo.values():
        for wt in worktrees:
            if wt.is_main or wt.branch in PROTECTED_BRANCHES:
                continue
            if wt.removable:
                candidates.append(wt)
                continue
            reason = []
            if wt.dirty:
                reason.append("dirty")
            if wt.locked:
                reason.append("locked")
            if not wt.merged_into:
                reason.append("not merged into main/test")
            if reason:
                skipped_notes.append(f"  skip {wt.path} ({', '.join(reason)})")

    if skipped_notes:
        print("Not offered for removal:")
        print("\n".join(skipped_notes))
        print()

    if not candidates:
        print("No worktrees are fully merged into main/test. Nothing to do.")
        return

    print(f"Worktrees merged into {remote}/main or {remote}/test (removable):")
    for wt in candidates:
        db_note = f", dbs: {', '.join(sorted(wt.db_names))}" if wt.db_names and not keep_dbs else ""
        print(f"  {wt.path}  (repo: {wt.repo}, branch: {wt.branch}{db_note})")

    if not yes:
        print("\nPass --yes to remove the above.")
        return

    drop_dbs = not keep_dbs and any(wt.db_names for wt in candidates)
    if drop_dbs and not testdb.confirm_remote_host(pg_host, pg_port):
        print("Aborted: database host not confirmed.")
        return

    touched_repos: set[Path] = set()
    for wt in candidates:
        result, db_results = remove_worktree(wt, drop_dbs=not keep_dbs, pg_host=pg_host, pg_port=pg_port, pg_user=pg_user)
        if result.returncode == 0:
            print(f"removed {wt.path}")
        else:
            print(f"FAILED to remove {wt.path}: {result.stderr.strip()}")
        for db, ok in db_results:
            print(f"  {'dropped' if ok else 'FAILED to drop'} db {db}")
        touched_repos.add(wt.repo)

    for repo in touched_repos:
        _run_capture(["git", "-C", str(repo), "worktree", "prune"])


# --- `bdt cleanup orphaned-dbs` ------------------------------------------


@dataclass(frozen=True)
class OrphanedDb:
    name: str
    project: str
    caution: bool


def find_orphaned_dbs(root: Path) -> list[OrphanedDb]:
    """Sweep every pgdevkit-postgres repo found under `root` (and any
    `db_nested_projects` it configures) for databases with no matching live
    git worktree. The actual diffing is entirely pgdevkit's own
    `find_orphaned_dbs()` (one call per project root) -- this just fans it
    out across every repo under `root` and layers bdt's own caution-DB
    heuristic on top (see `testdb.py`'s module docstring)."""
    orphaned: list[OrphanedDb] = []
    for repo in sorted(find_repos(root)):
        orphaned.extend(OrphanedDb(name, project, caution) for name, project, caution in testdb.find_orphaned(repo))
    return orphaned


def clean_orphaned_dbs(root: Path, *, include_caution: bool, yes: bool, pg_host: str, pg_port: int, pg_user: str) -> None:
    """Sweep for pgdevkit test DBs whose worktree no longer exists (e.g.
    removed by hand, or before this tool existed) across every repo found
    under `root`, and drop them -- but only when `yes` is set. DBs whose
    suffix looks like a bare branch name (main/test/dev/...) rather than a
    slugified feature branch are flagged as possibly-standing-reference and
    excluded unless `include_caution` is also set, since those might be
    intentional baseline DBs rather than orphaned leftovers.

    Unlike `clean_worktrees`, this has no notion of a `remote` to check
    "merged into" status against -- pgdevkit's orphan detection only cares
    whether a live git worktree exists for a branch at all, not whether
    that branch has been merged anywhere."""
    root = root.resolve()
    if not find_repos(root):
        print(f"No git repositories found under {root}.")
        return

    orphaned = find_orphaned_dbs(root)
    if not orphaned:
        print("No orphaned pgdevkit test DBs found.")
        return

    print("Orphaned pgdevkit test DBs (no matching live worktree):")
    for o in sorted(orphaned, key=lambda o: o.name):
        warn = "  ⚠ possibly a standing reference DB, verify first (pass --include-caution to include it)" if o.caution else ""
        print(f"  {o.name} ({o.project}){warn}")

    to_drop = [o for o in orphaned if include_caution or not o.caution]
    skipped = [o for o in orphaned if o not in to_drop]

    if not yes:
        hint = "\nPass --yes to drop the above"
        hint += "." if include_caution else " (excluding flagged ones; add --include-caution to include those too)."
        print(hint)
        return

    if to_drop and not testdb.confirm_remote_host(pg_host, pg_port):
        print("Aborted: database host not confirmed.")
        return

    for o in to_drop:
        result = testdb.drop_database(o.name, pg_host, pg_port, pg_user)
        print(f"{'dropped' if result.returncode == 0 else 'FAILED to drop'} db {o.name}")

    if skipped:
        print(f"\n{len(skipped)} flagged DB(s) not dropped (pass --include-caution to include them): {', '.join(o.name for o in skipped)}")


# --- `bdt cleanup db` ------------------------------------------------------


def clean_current_db(repo: Path, *, confirm: bool, pg_host: str, pg_port: int, pg_user: str) -> None:
    """Drop the pgdevkit test DB(s) owned by `repo` -- meant to be run from
    inside a live worktree (default `repo` is `.`), unlike `clean_worktrees`
    this never touches the worktree itself, only its database(s).

    Requires an explicit human confirmation: either `confirm=True` (bdt's
    `--confirm` flag) or an interactive 'yes' typed at the prompt below. On
    top of that, dropping anything on a non-local `--pg-host` additionally
    requires `testdb.confirm_remote_host()`'s own interactive confirmation,
    which `confirm=True` does *not* bypass.
    """
    db_names = testdb.workspace_db_names(repo)
    if not db_names:
        print(f"No pgdevkit test DB(s) found for {repo}.")
        return

    print(f"Test DB(s) for {repo}:")
    for name in sorted(db_names):
        print(f"  {name}")

    if not confirm:
        try:
            answer = input("\nDrop the above database(s)? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return

    if not testdb.confirm_remote_host(pg_host, pg_port):
        print("Aborted: database host not confirmed.")
        return

    for name in sorted(db_names):
        result = testdb.drop_database(name, pg_host, pg_port, pg_user)
        print(f"{'dropped' if result.returncode == 0 else 'FAILED to drop'} db {name}")


# --- `bdt cleanup worktree` -------------------------------------------------


def clean_current_worktree(path: Path, *, confirm: bool, keep_db: bool, pg_host: str, pg_port: int, pg_user: str) -> None:
    """Remove the single worktree at `path` (default: the current directory)
    plus, unless `keep_db`, its own pgdevkit test DB(s) -- meant to be run
    from inside that worktree, mirroring `clean_current_db`. Unlike
    `clean_worktrees`, this is an explicit, single-worktree teardown: it
    doesn't require "merged into main/test" status, only that `path` isn't
    the main checkout, isn't on a protected branch, and is clean (submodules
    included -- see `_is_dirty`).

    Removal itself goes through `remove_worktree()`, so a worktree
    containing submodules is handled the same way as in `clean_worktrees`:
    git's blanket "working trees containing submodules cannot be moved or
    removed" refusal is retried once with `--force`, since this worktree
    has already been verified clean before that point.

    Requires an explicit human confirmation: either `confirm=True` (bdt's
    `--confirm` flag) or an interactive 'yes' typed at the prompt below. On
    top of that, dropping its DB(s) on a non-local `--pg-host` additionally
    requires `testdb.confirm_remote_host()`'s own interactive confirmation,
    which `confirm=True` does *not* bypass.
    """
    path = path.resolve()
    main_root = _main_repo_root(path)
    if main_root is None:
        sys.exit(f"{path} is not inside a git worktree.")

    entries = _parse_worktree_list(main_root)
    if not entries:
        sys.exit(f"Could not list worktrees for {main_root}.")
    if Path(entries[0]["path"]).resolve() == path:
        sys.exit(f"Refusing to remove the main checkout at {path}.")

    entry = next((e for e in entries if Path(e["path"]).resolve() == path), None)
    if entry is None:
        sys.exit(f"{path} is not a registered worktree of {main_root}.")

    branch = entry.get("branch")
    if branch in PROTECTED_BRANCHES:
        sys.exit(f"Refusing to remove {path}: branch '{branch}' is protected.")
    if entry.get("locked"):
        sys.exit(f"Refusing to remove {path}: it is locked.")
    if _is_dirty(path):
        sys.exit(f"Refusing to remove {path}: it has uncommitted changes (submodules included).")

    db_names = frozenset() if keep_db else testdb.workspace_db_names(path)
    wt = Worktree(
        repo=main_root,
        path=path,
        head=entry.get("head", ""),
        branch=branch,
        is_main=False,
        locked=False,
        db_names=db_names,
    )

    print(f"Worktree: {wt.path} (repo: {wt.repo}, branch: {wt.branch})")
    if db_names:
        print(f"Test DB(s): {', '.join(sorted(db_names))}")

    if not confirm:
        suffix = " and its database(s)" if db_names else ""
        try:
            answer = input(f"\nRemove the above worktree{suffix}? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return

    if db_names and not testdb.confirm_remote_host(pg_host, pg_port):
        print("Aborted: database host not confirmed.")
        return

    result, db_results = remove_worktree(wt, drop_dbs=bool(db_names), pg_host=pg_host, pg_port=pg_port, pg_user=pg_user)
    if result.returncode != 0:
        sys.exit(f"FAILED to remove {wt.path}: {result.stderr.strip()}")
    print(f"removed {wt.path}")
    for db, ok in db_results:
        print(f"  {'dropped' if ok else 'FAILED to drop'} db {db}")
    _run_capture(["git", "-C", str(main_root), "worktree", "prune"])
