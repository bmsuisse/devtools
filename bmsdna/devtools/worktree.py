"""Create a git worktree for a new branch, mirroring the `just worktree`
recipes -- and, separately, `bdt cleanup worktrees` / `bdt cleanup
orphaned-dbs`: pruning worktrees already merged into main/test (and their
pgdevkit-managed Postgres test DB(s), see testdb.py), and sweeping for test
DBs whose worktree is already gone some other way.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

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
    project_name = testdb.read_pgdevkit_project(repo)

    worktrees: list[Worktree] = []
    for i, e in enumerate(entries):
        if e.get("bare"):
            continue
        path = Path(e["path"])
        head = e.get("head", "")
        branch = e.get("branch")
        db_names = testdb.expected_db_names(repo, project_name, branch) if project_name and branch else frozenset()
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


def remove_worktree(wt: Worktree, *, drop_dbs: bool, pg_port: int, pg_user: str) -> tuple[subprocess.CompletedProcess, list[tuple[str, bool]]]:
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
            dr = testdb.drop_database(db, pg_port, pg_user)
            db_results.append((db, dr.returncode == 0))
    return result, db_results


def clean_worktrees(root: Path, *, remote: str, keep_dbs: bool, yes: bool, pg_port: int, pg_user: str) -> None:
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

    touched_repos: set[Path] = set()
    for wt in candidates:
        result, db_results = remove_worktree(wt, drop_dbs=not keep_dbs, pg_port=pg_port, pg_user=pg_user)
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


def find_orphaned_dbs(by_repo: dict[Path, list[Worktree]], *, pg_port: int, pg_user: str) -> list[OrphanedDb]:
    """Diff actual Postgres DBs against the DBs every *currently existing*
    worktree (regardless of merge status) is expected to own. Anything left
    over belongs to a worktree that's already gone (removed by hand, or
    before this tool tracked DB cleanup)."""
    live_expected: set[str] = set()
    project_names: set[str] = set()
    sibling_suffixes_by_project: dict[str, list[str]] = {}
    for repo, worktrees in by_repo.items():
        project_name = testdb.read_pgdevkit_project(repo)
        if not project_name:
            continue
        project_names.add(project_name)
        sibling_suffixes_by_project[project_name] = testdb.db_sibling_suffixes(repo)
        project_names.update(testdb.db_nested_projects(repo))
        for wt in worktrees:
            if wt.branch:
                live_expected |= testdb.expected_db_names(repo, project_name, wt.branch)

    if not project_names:
        return []

    prefixes = tuple(f"{p}_" for p in project_names)
    orphaned = []
    for db in testdb.list_databases(pg_port, pg_user):
        if not db.startswith(prefixes):
            continue
        if db in live_expected:
            continue
        owning_project = max((p for p in project_names if db.startswith(f"{p}_")), key=len)
        siblings = sibling_suffixes_by_project.get(owning_project, [])
        orphaned.append(OrphanedDb(db, owning_project, testdb.is_caution_db(db, owning_project, siblings)))
    return orphaned


def clean_orphaned_dbs(root: Path, *, remote: str, include_caution: bool, yes: bool, pg_port: int, pg_user: str) -> None:
    """Sweep for pgdevkit test DBs whose worktree no longer exists (e.g.
    removed by hand, or before this tool existed) across every repo found
    under `root`, and drop them -- but only when `yes` is set. DBs whose
    suffix looks like a bare branch name (main/test/dev/...) rather than a
    slugified feature branch are flagged as possibly-standing-reference and
    excluded unless `include_caution` is also set, since those might be
    intentional baseline DBs rather than orphaned leftovers."""
    root = root.resolve()
    repos = find_repos(root)
    by_repo = {repo: collect_worktrees(repo, remote) for repo in sorted(repos)}

    orphaned = find_orphaned_dbs(by_repo, pg_port=pg_port, pg_user=pg_user)
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

    for o in to_drop:
        result = testdb.drop_database(o.name, pg_port, pg_user)
        print(f"{'dropped' if result.returncode == 0 else 'FAILED to drop'} db {o.name}")

    if skipped:
        print(f"\n{len(skipped)} flagged DB(s) not dropped (pass --include-caution to include them): {', '.join(o.name for o in skipped)}")
