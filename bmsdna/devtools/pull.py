"""`bdt pull`: bring the current branch up to date from three sources, each a
separate `git pull` so a conflict in one is reported before the next runs
rather than piling every source into one merge no one asked for:

1. the current branch's own remote tracking branch (plain `git pull`);
2. the remote's `main` or `master` branch, whichever exists;
3. the repo's DEFAULT branch, as configured on the remote -- unless
   `--no-default`. Skipped (rather than run again) when it turns out to be
   the same branch as step 2, which is the common case.

"Default branch" here is resolved via `git ls-remote --symref <remote> HEAD`
(what `git remote show <remote>`'s "HEAD branch" line is itself built from)
rather than `gh repo view`: it's plain git, so it works the same for a GitHub
or an Azure DevOps remote and needs no extra CLI/auth beyond what a `git
pull` already needs. Step 2 (main/master) is a separate, more conservative
check on top of that -- it only ever looks at branches literally named
`main`/`master`, so it still does something sane on repos whose configured
default branch is neither (e.g. `develop`).

On a merge conflict, this stops immediately (later steps are skipped) and
exits with the exact `bdt pull ...` command to re-run once the conflict is
resolved -- same flags as this invocation, minus `--dry-run`.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .cli_tools import CLI_TIMEOUT_SECS
from .gitrepo import current_branch as _current_branch

# `git pull` can legitimately take longer than a metadata call (`ls-remote`,
# `rev-parse`) -- an actual fetch+merge of a large repo over a slow network --
# so it gets a more generous timeout than everything else in this module,
# same reasoning as `CLI_UPLOAD_TIMEOUT_SECS` in gh_pr.py.
PULL_TIMEOUT_SECS = CLI_TIMEOUT_SECS * 4


def _run_capture(cmd: list[str], cwd: Path | str | None = None, *, timeout: float = CLI_TIMEOUT_SECS) -> subprocess.CompletedProcess:
    """`subprocess.run` bounded by `timeout` -- every `git` call in this module goes
    through this, so a stalled network call (or git blocking on an interactive
    prompt, e.g. an expired SSH/HTTPS credential) can't hang the caller forever.
    A timeout is reported the same way as any other non-zero exit (returncode
    124, stderr set) rather than raising, so callers that already treat "git
    failed" as "couldn't determine this, fall back" (e.g. `default_branch`)
    don't need special-casing for it.
    """
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout)
    except FileNotFoundError:
        sys.exit(f"'{cmd[0]}' is required for this command but wasn't found on PATH.")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, 124, "", f"`{' '.join(cmd)}` timed out after {timeout:.0f}s -- stalled network, or needs an interactive login?"
        )


def current_branch(cwd: Path | str | None = None) -> str:
    return _current_branch(str(cwd) if cwd is not None else None)


def upstream_branch(cwd: Path | str | None = None) -> str | None:
    """The current branch's configured remote tracking branch (e.g.
    'origin/my-feature'), or None if it has none set."""
    result = _run_capture(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)
    return result.stdout.strip() if result.returncode == 0 else None


def _parse_ls_remote_heads(output: str) -> dict[str, str]:
    branches: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            branches[parts[1].removeprefix("refs/heads/")] = parts[0]
    return branches


def _query_remote(remote: str, cwd: Path | str | None, *, timeout: float) -> tuple[str | None, dict[str, str]]:
    """One `git ls-remote --symref <remote> HEAD main master` covering both
    `default_branch`'s and `remote_main_or_master`'s needs in a single
    network round trip -- returns (HEAD's symref target, {branch: sha}).
    ({} and None respectively if the remote couldn't be reached.)
    """
    result = _run_capture(["git", "ls-remote", "--symref", remote, "HEAD", "main", "master"], cwd, timeout=timeout)
    if result.returncode != 0:
        return None, {}
    default = None
    for line in result.stdout.splitlines():
        # "ref: refs/heads/main\tHEAD"
        if line.startswith("ref:") and line.endswith("HEAD"):
            default = line[len("ref:"):].split("\t")[0].strip().removeprefix("refs/heads/")
    return default, _parse_ls_remote_heads(result.stdout)


def remote_main_or_master(remote: str, cwd: Path | str | None = None) -> str | None:
    """'main' or 'master', whichever exists as a branch on `remote` (checked
    live via `git ls-remote`, not stale local remote-tracking refs); 'main'
    wins if somehow both exist. None if neither does, or the remote couldn't
    be reached."""
    _, branches = _query_remote(remote, cwd, timeout=CLI_TIMEOUT_SECS)
    if "main" in branches:
        return "main"
    if "master" in branches:
        return "master"
    return None


def default_branch(remote: str, cwd: Path | str | None = None, *, timeout: float = CLI_TIMEOUT_SECS) -> str | None:
    """The branch `remote`'s HEAD points at -- i.e. its configured default
    branch -- or None if it couldn't be determined (remote unreachable, or
    it has no HEAD symref, e.g. an empty repo).

    `timeout` defaults to a full `CLI_TIMEOUT_SECS`, but a caller for whom this
    is only a best-effort nicety rather than the actual point of the command
    (e.g. `worktree.create()`'s post-creation hint) should pass a short one --
    an offline/slow remote shouldn't make an unrelated command visibly stall.
    """
    default, _ = _query_remote(remote, cwd, timeout=timeout)
    return default


# A recent-ish git refuses to even attempt a `git pull` that isn't a fast-forward
# unless it's told how to reconcile divergent branches (merge vs rebase vs
# fast-forward-only) -- either via `pull.rebase`/`pull.ff` in the caller's git
# config, or one of these flags on the command line. A caller with neither
# configured would otherwise have `bdt pull` immediately fail every step with
# git's "Need to specify how to reconcile divergent branches" instead of doing
# anything, which defeats the point of the command. `--no-rebase` (a plain
# merge, never rewriting history) is added by default so it always does
# something sensible out of the box; any of these flags in `pull_args`
# overrides that default instead of stacking with it.
_STRATEGY_FLAGS = ("--rebase", "--no-rebase", "--ff-only", "--ff", "--no-ff")


def _with_default_strategy(pull_args: list[str]) -> list[str]:
    if any(a in _STRATEGY_FLAGS or a.startswith("--rebase=") for a in pull_args):
        return pull_args
    return ["--no-rebase", *pull_args]


@dataclass(frozen=True)
class PullStep:
    label: str
    # None means "nothing to do" (e.g. no upstream configured) -- the step is
    # reported as skipped rather than run.
    cmd: list[str] | None


def build_steps(remote: str, *, no_default: bool, pull_args: list[str], cwd: Path | str | None = None) -> list[PullStep]:
    steps: list[PullStep] = []
    args = _with_default_strategy(pull_args)

    if upstream_branch(cwd) is not None:
        steps.append(PullStep("current branch's remote tracking branch", ["git", "pull", *args]))
    else:
        steps.append(PullStep("current branch's remote tracking branch (skipped: no upstream configured)", None))

    # One combined `git ls-remote` covers both main/master and the default
    # branch -- see `_query_remote` -- rather than two separate round trips
    # to the same remote.
    default, branches = _query_remote(remote, cwd, timeout=CLI_TIMEOUT_SECS)
    main = "main" if "main" in branches else "master" if "master" in branches else None
    if main is not None:
        steps.append(PullStep(f"{remote}/{main}", ["git", "pull", remote, main, *args]))
    else:
        steps.append(PullStep(f"{remote}'s main/master branch (skipped: neither exists on {remote}, or it's unreachable)", None))

    if not no_default:
        if default is not None and default == main:
            # Common case: the default branch IS main/master, already pulled
            # above -- running the identical `git pull` a second time would
            # just print "Already up to date.", so skip it instead.
            steps.append(PullStep(f"{remote}'s default branch ({default}) (skipped: same as {remote}/{main}, already pulled above)", None))
        elif default is not None:
            steps.append(PullStep(f"{remote}'s default branch ({default})", ["git", "pull", remote, default, *args]))
        else:
            steps.append(PullStep(f"{remote}'s default branch (skipped: could not be determined)", None))

    return steps


def _has_conflict(cwd: Path | str | None) -> bool:
    """True if the working tree currently has unmerged (conflicted) paths --
    left behind by a `git pull` that hit a merge/rebase conflict, regardless
    of which strategy `pull_args` picked."""
    result = _run_capture(["git", "ls-files", "-u"], cwd)
    return bool(result.stdout.strip())


def rerun_command(remote: str, *, no_default: bool, pull_args: list[str]) -> str:
    """The exact `bdt pull ...` invocation to print alongside a conflict
    error -- same flags as the current run, minus --dry-run (resolving a
    conflict means actually pulling, not previewing again)."""
    parts = ["bdt", "pull"]
    if remote != "origin":
        parts += ["--remote", remote]
    if no_default:
        parts.append("--no-default")
    if pull_args:
        parts += ["--", *pull_args]
    return " ".join(shlex.quote(p) for p in parts)


def run(
    *,
    remote: str = "origin",
    no_default: bool = False,
    dry_run: bool = False,
    pull_args: list[str] | None = None,
    cwd: Path | str | None = None,
) -> None:
    pull_args = pull_args or []

    # Checked up front, before any step runs, so that a conflict seen after a
    # step below is guaranteed to have been *caused* by that step -- without
    # this, `_has_conflict` can't tell a conflict this run just made from one
    # already sitting there from an earlier, unrelated, still-unresolved
    # `git rebase`/`cherry-pick`/`bdt pull`, and would mislabel the latter as
    # caused by whichever step happens to run (and fail) first.
    if _has_conflict(cwd):
        sys.exit(
            "ERROR: this worktree already has unresolved merge conflicts (unrelated to this run).\n"
            "Resolve them (or run `git merge --abort` / `git rebase --abort` to give up), then re-run `bdt pull`."
        )

    steps = build_steps(remote, no_default=no_default, pull_args=pull_args, cwd=cwd)

    print(f"bringing '{current_branch(cwd)}' up to date from {remote}:")
    for step in steps:
        if step.cmd is None:
            print(f"skip: {step.label}")
            continue

        cmd_str = " ".join(shlex.quote(c) for c in step.cmd)
        if dry_run:
            print(f"would run: {cmd_str}")
            continue

        print(f"pulling {step.label}: {cmd_str}")
        result = _run_capture(step.cmd, cwd, timeout=PULL_TIMEOUT_SECS)
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.returncode == 0:
            continue

        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)

        if _has_conflict(cwd):
            rerun = rerun_command(remote, no_default=no_default, pull_args=pull_args)
            sys.exit(
                f"ERROR: merge conflict while pulling {step.label}.\n"
                f"Resolve the conflict(s) (or run `git merge --abort` / `git rebase --abort` to give up), "
                f"then re-run:\n  {rerun}"
            )

        sys.exit(f"ERROR: `{cmd_str}` failed (see output above).")

    print("dry run complete; nothing was pulled" if dry_run else "done")
