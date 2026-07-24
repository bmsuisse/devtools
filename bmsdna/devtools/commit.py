"""Commit-and-push helper: pre-flight checks, one retry after a pre-commit
reformat, optional JSON output for AI-agent callers, optional subrepo split
for repos that vendor a submodule (e.g. a `database/` git submodule).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

IS_SANDBOX_ENV_VAR = "IS_BMS_AI_SANDBOX"


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, encoding="utf-8", cwd=cwd)
    except FileNotFoundError:
        sys.exit("'git' is required for this command but wasn't found on PATH.")


def _sha(cwd: str | None = None) -> str | None:
    r = _run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else None


def _in_sandbox() -> bool:
    return os.getenv(IS_SANDBOX_ENV_VAR, "0").lower() in ("1", "true", "yes")


def _staged_deletion(path: str, cwd: str | None = None) -> bool:
    """True if `path` is already staged as a deletion (e.g. via `git rm`) in
    the repo at `cwd`. This is a deliberate-intent signal, not just "HEAD used
    to have this path" — a file that vanished because a write failed or raced
    would be missing from disk but NOT staged as a deletion, so it still
    fails the check below instead of silently being committed as removed."""
    r = _run(["git", "diff", "--cached", "--name-only", "--diff-filter=D", "--", path], cwd=cwd)
    return r.returncode == 0 and path in r.stdout.splitlines()


def _present_or_staged_deletion(path: str, subrepos: list[str]) -> bool:
    if os.path.exists(path):
        return True
    for subrepo in subrepos:
        prefix = subrepo + "/"
        if path.startswith(prefix):
            return _staged_deletion(path[len(prefix):], cwd=subrepo)
    return _staged_deletion(path)


@dataclass
class CommitResult:
    success: bool
    committed: bool
    pushed: bool
    message: str
    files: list[str]
    commit_sha: str | None = None
    error: str | None = None
    hint: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "success": self.success,
            "committed": self.committed,
            "pushed": self.pushed,
            "message": self.message,
            "files": self.files,
            "commit_sha": self.commit_sha,
            "error": self.error,
            "hint": self.hint,
        }
        d.update(self.extra)
        return d


def _git_commit(message: str, cwd: str | None = None, no_verify: bool = False) -> subprocess.CompletedProcess:
    cmd = ["git", "commit", "-m", message]
    if no_verify:
        cmd.append("--no-verify")
    return _run(cmd, cwd=cwd)


def _add(files: list[str], cwd: str | None) -> None:
    """`git add`, excluding paths already fully removed (disk + index) by a
    prior `git rm` -- those have nothing left to add, and `git add` fails its
    ENTIRE invocation (staging nothing, for any path in the same call) if even
    one pathspec doesn't match anything. Passing only what's actually present
    on disk avoids silently dropping the other files' staged content."""
    root = cwd or "."
    addable = [f for f in files if os.path.exists(os.path.join(root, f))]
    if addable:
        _run(["git", "add", *addable], cwd=cwd)


def _commit_with_retry(
    message: str, files: list[str], cwd: str | None, no_verify: bool
) -> tuple[bool, subprocess.CompletedProcess | None]:
    """Commit, retrying once (re-`git add`) if a pre-commit hook reformatted files."""
    _add(files, cwd)
    r = _git_commit(message, cwd=cwd, no_verify=no_verify)
    if r.returncode == 0:
        return True, None
    if "nothing to commit" in r.stdout + r.stderr:
        return False, None
    _add(files, cwd)
    r2 = _git_commit(message, cwd=cwd, no_verify=no_verify)
    if r2.returncode == 0:
        return True, None
    return False, r2


def commit_and_push(
    message: str,
    files: list[str],
    *,
    no_verify: bool = False,
    require_message_quality: bool = True,
    require_feature_branch: bool = True,
    subrepos: list[str] | None = None,
) -> CommitResult:
    """Stage, commit, and push `files`, applying the same pre-flight checks
    and pre-commit-hook retry as the per-repo `commit.py` scripts.

    `subrepos` is a list of submodule directory names (e.g. ["database"]);
    files under one of those prefixes are committed/pushed inside the
    submodule first, then the submodule bump is staged in the parent repo.
    """
    # Normalize to forward slashes so subrepo-prefix matching below works the
    # same whether a caller passes "database/x.sql" or "database\x.sql" (both
    # os.path.exists and git accept either separator fine on Windows).
    files = [f.replace("\\", "/") for f in files]
    subrepos = subrepos or []
    print("pre-flight checks:", flush=True)

    def check(ok: bool, label: str) -> bool:
        print(f"  {'✓' if ok else '✗'} {label}", file=sys.stdout if ok else sys.stderr)
        return ok

    missing = [f for f in files if not _present_or_staged_deletion(f, subrepos)]
    if not check(not missing, "files exist (or are a staged deletion)"):
        return CommitResult(
            False, False, False, message, files,
            error=f"File not found: {missing[0]} — did you typo the path? Run `git status` to see changed files",
            hint=f"Run `git status` to see what files are actually changed. Missing: {missing}",
        )

    msg_ok = not require_message_quality or (len(message) >= 20 and ":" in message)
    if not check(msg_ok, "commit message quality (len>=20, has colon)"):
        return CommitResult(
            False, False, False, message, files,
            error=f"Commit message too short or missing type prefix (e.g. 'feat(x): ...') — got: {message!r}",
            hint="Use conventional commits format: 'feat(scope): description' or 'fix: description'",
        )

    current_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    branch_ok = not require_feature_branch or current_branch not in ("main", "master")
    if not check(branch_ok, f"not on main/master (branch: {current_branch})"):
        return CommitResult(
            False, False, False, message, files,
            error=f"Direct push to {current_branch} blocked — create a feature branch first",
            hint="Run `git checkout -b feat/my-branch` to create a feature branch first.",
        )

    main_files = list(files)
    for subrepo in subrepos:
        prefix = subrepo + "/"
        subrepo_files = [f[len(prefix):] for f in files if f.startswith(prefix)]
        main_files = [f for f in main_files if not f.startswith(prefix) and f != subrepo]
        if not subrepo_files:
            continue

        ok, failure = _commit_with_retry(message, subrepo_files, cwd=subrepo, no_verify=no_verify)
        if failure is not None:
            return CommitResult(
                False, False, False, message, subrepo_files,
                error=(failure.stdout + failure.stderr).strip(),
                hint="Pre-commit hook may have failed in the subrepo. Check the error output above.",
            )
        if ok and not _in_sandbox():
            pr = _run(["git", "push"], cwd=subrepo)
            if pr.returncode != 0:
                return CommitResult(
                    False, True, False, message, subrepo_files,
                    commit_sha=_sha(cwd=subrepo),
                    error=(pr.stdout + pr.stderr).strip(),
                    hint=f"Push failed. Try `git pull --rebase` in the {subrepo} submodule.",
                )
            print(f"  ✓ {subrepo} subrepo pushed", flush=True)
        if ok:
            main_files.append(subrepo)

    ok, failure = _commit_with_retry(message, main_files, cwd=None, no_verify=no_verify)
    if failure is not None:
        return CommitResult(
            False, False, False, message, files,
            error=(failure.stdout + failure.stderr).strip(),
            hint="Pre-commit hook may have reformatted files and failed. Check output.",
        )
    committed = ok
    print("  ✓ committed", flush=True)

    if _in_sandbox():
        return CommitResult(True, committed, False, message, files, commit_sha=_sha())

    pr = _run(["git", "push"])
    if pr.returncode != 0:
        return CommitResult(
            False, committed, False, message, files,
            commit_sha=_sha(),
            error=(pr.stdout + pr.stderr).strip(),
            hint="Push rejected. Run `git pull --rebase`, resolve conflicts, then retry.",
        )

    print("  ✓ pushed", flush=True)
    return CommitResult(True, committed, True, message, files, commit_sha=_sha())


def emit(result: CommitResult, *, use_json: bool) -> None:
    if use_json:
        print(json.dumps(result.as_dict(), indent=2))
    elif result.success:
        if not result.committed:
            print("nothing to commit")
        elif not result.pushed:
            print("committed (sandbox mode, push handled separately)")
        else:
            print("committed & pushed")
    else:
        print(f"ERROR: {result.error}", file=sys.stderr)
        if result.hint:
            print(f"HINT: {result.hint}", file=sys.stderr)
    sys.exit(0 if result.success else 1)
