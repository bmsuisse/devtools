"""Target-branch drift of an Azure DevOps PR: does it still merge now that its target moved on?

Azure DevOps computes a PR's `mergeStatus` (and the merge commit its builds run on) once per push to the
*source* branch and never again when the *target* branch gains commits, so a PR can report `succeeded` for
weeks while the target has since changed the same lines. `check` notices that (the target's tip is not the
`lastMergeTargetCommit` the status was computed against) and merges the two locally with `git merge-tree`,
which touches neither the working tree nor any ref. GitHub recomputes `mergeable` itself, so needs none of this.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .cli_tools import CLI_TIMEOUT_SECS
from .gitrepo import AdoRemote, UnknownRemoteError, current_remote

# Fetching objects over the network is slower than the metadata calls `CLI_TIMEOUT_SECS` is meant for.
FETCH_TIMEOUT_SECS = CLI_TIMEOUT_SECS * 4

# A check's answer only depends on the three commits, so it is kept between runs (a status line polls `pr info`).
_CACHE_FILE = "bdt-merge-check.json"
_CACHE_ENTRIES = 50

_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class Drift:
    """The target moved since the PR's merge status was computed."""

    target: str
    behind: int | None  # commits the target gained since then; None if they couldn't be counted
    conflicts: tuple[str, ...] | None  # paths that conflict with the target's tip; () = merges cleanly, None = couldn't tell

    @property
    def conflicting(self) -> bool:
        return bool(self.conflicts)


def _git(args: list[str], cwd: str | None, timeout: float = CLI_TIMEOUT_SECS) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:  # no git, or a fetch that hangs: "couldn't tell", like any other failure
        return subprocess.CompletedProcess(["git", *args], 255, "", str(e))


def _cache_path(cwd: str | None) -> Path | None:
    r = _git(["rev-parse", "--git-path", _CACHE_FILE], cwd)
    return Path(cwd or ".") / r.stdout.strip() if r.returncode == 0 else None


def _cache_read(path: Path | None) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}


def _cache_write(path: Path | None, cache: dict) -> None:
    if path is None:
        return
    try:
        path.write_text(json.dumps(dict(list(cache.items())[-_CACHE_ENTRIES:])), encoding="utf-8")
    except OSError:
        pass  # a cache that can't be written only costs the next run a fetch


def merge_conflicts(source: str, target: str, cwd: str | None = None) -> tuple[str, ...] | None:
    """Paths that conflict when merging commit `source` into commit `target`; `()` if it merges cleanly,
    `None` if that couldn't be found out (an object missing locally, git too old for `merge-tree --write-tree`)."""
    r = _git(["merge-tree", "--write-tree", "--name-only", "--no-messages", target, source], cwd)
    if r.returncode == 0:
        return ()
    lines = r.stdout.splitlines()
    # a conflict is exit 1 with the merged tree's id first and one conflicted path per line after it; an unknown commit is exit 1 too
    if r.returncode != 1 or not lines or not _OBJECT_ID.fullmatch(lines[0]):
        return None
    return tuple(dict.fromkeys(line for line in lines[1:] if line))


def _is_this_repo(remote: AdoRemote, cwd: str | None) -> bool:
    try:
        return current_remote(cwd) == remote
    except (SystemExit, UnknownRemoteError):
        return False


def moved_target(pr: dict) -> tuple[str, str, str] | None:
    """(target branch, commit its merge status was computed against, source commit) of an active `pr`; None if it has none."""
    target = (pr.get("targetRefName") or "").removeprefix("refs/heads/")
    last_target = (pr.get("lastMergeTargetCommit") or {}).get("commitId")
    source = (pr.get("lastMergeSourceCommit") or {}).get("commitId")
    if pr.get("status") != "active" or not (target and last_target and source):
        return None
    return target, last_target, source


def check(remote: AdoRemote, pr: dict, tip: str | None, cwd: str | None = None) -> Drift | None:
    """None unless the target branch of `pr` (see `moved_target`) has moved since its merge status was computed, i.e.
    `tip`, the commit the branch points at now, isn't the one that status is about.

    Needs a checkout of `remote` for the merge itself (`cwd`, default the current directory); for a PR of another
    repo, or when the commits can't be fetched, the answer is a `Drift` whose `conflicts` is None: the target moved, but
    whether that matters is unknown.
    """
    moved = moved_target(pr)
    if moved is None or tip is None or tip == moved[1]:
        return None
    target, last_target, source = moved
    if not _is_this_repo(remote, cwd):
        return Drift(target, None, None)

    key = f"{last_target}:{tip}:{source}"
    path = _cache_path(cwd)
    cache = _cache_read(path)
    if key in cache:
        behind, conflicts = cache[key]
        return Drift(target, behind, None if conflicts is None else tuple(conflicts))

    # by commit id: the PR's source branch may be one this checkout never fetched
    fetched = _git(["fetch", "--quiet", "--no-write-fetch-head", "origin", last_target, tip, source], cwd, FETCH_TIMEOUT_SECS)
    if fetched.returncode != 0:
        return Drift(target, None, None)  # not cached: a later run may manage to fetch
    count = _git(["rev-list", "--count", f"{last_target}..{tip}"], cwd)
    behind = int(count.stdout) if count.returncode == 0 else None
    conflicts = merge_conflicts(source, tip, cwd)
    cache[key] = [behind, None if conflicts is None else list(conflicts)]
    _cache_write(path, cache)
    return Drift(target, behind, conflicts)


def _moved(drift: Drift) -> str:
    return f"'{drift.target}' has moved on" + (f" by {drift.behind} commit{'s' * (drift.behind != 1)}" if drift.behind else "")


def conflict_message(pr: dict, drift: Drift) -> str:
    """`pr` conflicts with the target's tip: what to say, in the shape of `pr_build.merge_conflict_message`."""
    shown = ", ".join((drift.conflicts or ())[:10]) + (", ..." if len(drift.conflicts or ()) > 10 else "")
    return (
        f"PR #{pr.get('pullRequestId')} ({pr.get('title', '?')!r}) has merge conflicts with '{drift.target}' (mergeStatus="
        f"{pr.get('mergeStatus', '?')} is stale: {_moved(drift)} since it was computed). Conflicting: {shown}. "
        f"Merge or rebase '{drift.target}' into the branch and push."
    )


def notice(drift: Drift) -> str:
    """The target moved but `drift` isn't a conflict: informational, the PR still merges as far as is known."""
    outcome = "the PR still merges cleanly" if drift.conflicts == () else "not checked for conflicts"
    return f"Note: {_moved(drift)} since the PR's merge status and builds were computed; {outcome}."
