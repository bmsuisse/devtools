import subprocess

import pytest

from bmsdna.devtools.pull import (
    _run_capture,
    build_steps,
    current_branch,
    default_branch,
    remote_main_or_master,
    rerun_command,
    run,
    upstream_branch,
)


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    (path / "f.txt").write_text("base\n")
    _git(["add", "."], cwd=path)
    _git(["commit", "-q", "-m", "init"], cwd=path)
    return path


def clone(remote, path, checkout: str | None = None):
    """Clone `remote` (a plain, non-bare repo doubling as the test "remote")
    into `path`, then switch to `checkout` if given -- unless the clone
    already landed there, since a non-bare remote's HEAD (and so the branch
    `git clone` checks out by default) tracks whatever's currently checked
    out *in the remote itself*, which earlier setup in a test may already
    have moved to `checkout`."""
    _git(["clone", "-q", str(remote), str(path)], cwd=None)
    if checkout and _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path).stdout.strip() != checkout:
        _git(["checkout", "-q", "-b", checkout, f"origin/{checkout}"], cwd=path)
    return path


def commit_file(repo, content, message="change"):
    (repo / "f.txt").write_text(content)
    _git(["commit", "-q", "-am", message], cwd=repo)


# --- _run_capture timeout handling -------------------------------------------


def test_run_capture_converts_timeout_into_a_failed_completed_process_instead_of_hanging(tmp_path, monkeypatch) -> None:
    """A stalled network call (or git blocking on an interactive credential
    prompt) must not hang `bdt pull` forever -- it's reported the same way as
    any other git failure (non-zero returncode, message in stderr) instead of
    raising, so existing "git failed" handling (skip/fallback/error) already
    covers it without special-casing."""

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _run_capture(["git", "ls-remote", "--symref", "origin", "HEAD"], tmp_path, timeout=1.0)

    assert result.returncode == 124
    assert "timed out after 1s" in result.stderr


# --- current_branch / upstream_branch ---------------------------------------


def test_current_branch_returns_checked_out_branch(tmp_path) -> None:
    repo = init_repo(tmp_path / "repo")
    assert current_branch(repo) == "main"


def test_upstream_branch_none_when_not_tracking_anything(tmp_path) -> None:
    repo = init_repo(tmp_path / "repo")
    assert upstream_branch(repo) is None


def test_upstream_branch_reports_configured_tracking_branch(tmp_path) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    assert upstream_branch(checkout) == "origin/main"


# --- remote_main_or_master ---------------------------------------------------


def test_remote_main_or_master_finds_main(tmp_path) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    assert remote_main_or_master("origin", checkout) == "main"


def test_remote_main_or_master_falls_back_to_master(tmp_path) -> None:
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(["init", "-q", "-b", "master"], cwd=remote)
    _git(["config", "user.email", "test@example.com"], cwd=remote)
    _git(["config", "user.name", "Test"], cwd=remote)
    (remote / "f.txt").write_text("base\n")
    _git(["add", "."], cwd=remote)
    _git(["commit", "-q", "-m", "init"], cwd=remote)
    checkout = tmp_path / "clone"
    _git(["clone", "-q", str(remote), str(checkout)], cwd=None)

    assert remote_main_or_master("origin", checkout) == "master"


def test_remote_main_or_master_none_when_neither_exists(tmp_path) -> None:
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(["init", "-q", "-b", "trunk"], cwd=remote)
    _git(["config", "user.email", "test@example.com"], cwd=remote)
    _git(["config", "user.name", "Test"], cwd=remote)
    (remote / "f.txt").write_text("base\n")
    _git(["add", "."], cwd=remote)
    _git(["commit", "-q", "-m", "init"], cwd=remote)
    checkout = tmp_path / "clone"
    _git(["clone", "-q", str(remote), str(checkout)], cwd=None)

    assert remote_main_or_master("origin", checkout) is None


# --- default_branch -----------------------------------------------------------


def test_default_branch_resolves_remote_head_symref(tmp_path) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    assert default_branch("origin", checkout) == "main"


def test_default_branch_none_for_unreachable_remote(tmp_path) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    _git(["remote", "set-url", "origin", str(tmp_path / "does-not-exist")], cwd=checkout)

    assert default_branch("origin", checkout) is None


# --- build_steps ---------------------------------------------------------------


def test_build_steps_dedups_main_pull_when_it_matches_the_tracking_branch(tmp_path) -> None:
    """Running `bdt pull` while already on main/master itself -- whose tracking
    branch IS origin/main -- must not pull origin/main a second time in step 2:
    it's the exact ref step 1 (the bare `git pull`) already covers."""
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")  # stays on 'main', tracking origin/main

    steps = build_steps("origin", no_default=False, pull_args=[], cwd=checkout)

    assert [s.label for s in steps] == [
        "current branch's remote tracking branch",
        "origin/main (skipped: same as current branch's remote tracking branch, already pulled above)",
        "origin's default branch (main) (skipped: same as current branch's remote tracking branch, already pulled above)",
    ]
    assert steps[0].cmd is not None
    assert steps[1].cmd is None
    assert steps[2].cmd is None


def test_build_steps_dedups_default_branch_pull_when_it_matches_main(tmp_path) -> None:
    """The common case: the remote's DEFAULT branch IS main, but the current branch
    tracks something else entirely. Re-running the identical `git pull origin main`
    a second time (step 3 after step 2) would be a no-op -- skip it instead."""
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    _git(["checkout", "-q", "-b", "feature", "--no-track"], cwd=checkout)  # no upstream -- isolates step 2 vs 3

    steps = build_steps("origin", no_default=False, pull_args=[], cwd=checkout)

    assert [s.label for s in steps] == [
        "current branch's remote tracking branch (skipped: no upstream configured)",
        "origin/main",
        "origin's default branch (main) (skipped: same as origin/main, already pulled above)",
    ]
    assert steps[1].cmd is not None
    assert steps[2].cmd is None


def test_build_steps_includes_all_three_distinct_real_pulls_when_none_overlap(tmp_path) -> None:
    remote = init_repo(tmp_path / "remote")
    _git(["checkout", "-q", "-b", "develop"], cwd=remote)  # 'main' still exists; remote's default is now 'develop'
    checkout = clone(remote, tmp_path / "clone", checkout="develop")
    _git(["checkout", "-q", "-b", "feature"], cwd=checkout)
    _git(["push", "-q", "-u", "origin", "feature"], cwd=checkout)  # tracks origin/feature -- distinct from main and develop

    steps = build_steps("origin", no_default=False, pull_args=[], cwd=checkout)

    assert [s.label for s in steps] == [
        "current branch's remote tracking branch",
        "origin/main",
        "origin's default branch (develop)",
    ]
    for step in steps:
        assert step.cmd is not None
        assert "--no-rebase" in step.cmd


def test_build_steps_queries_the_remote_exactly_once_for_main_and_default_branch(tmp_path, monkeypatch) -> None:
    """`remote_main_or_master` and `default_branch`'s info (main/master heads, and
    the default branch) both come from a single `git ls-remote`, not two separate
    round trips to the same remote -- regression test for that dedup."""
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")

    import bmsdna.devtools.pull as pull_module

    ls_remote_calls: list[list[str]] = []
    real_run_capture = pull_module._run_capture

    def counting_run_capture(cmd, cwd=None, **kwargs):
        if "ls-remote" in cmd:
            ls_remote_calls.append(cmd)
        return real_run_capture(cmd, cwd, **kwargs)

    monkeypatch.setattr(pull_module, "_run_capture", counting_run_capture)

    build_steps("origin", no_default=False, pull_args=[], cwd=checkout)

    assert len(ls_remote_calls) == 1


def test_build_steps_skips_default_branch_when_no_default(tmp_path) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    _git(["checkout", "-q", "-b", "feature", "--no-track"], cwd=checkout)  # isolate from the tracking-branch dedup

    steps = build_steps("origin", no_default=True, pull_args=[], cwd=checkout)

    assert [s.label for s in steps] == [
        "current branch's remote tracking branch (skipped: no upstream configured)",
        "origin/main",
    ]


def test_build_steps_reports_skipped_tracking_branch_when_no_upstream(tmp_path) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    _git(["checkout", "-q", "-b", "detached-feature"], cwd=checkout)

    steps = build_steps("origin", no_default=True, pull_args=[], cwd=checkout)

    assert steps[0].cmd is None
    assert "no upstream configured" in steps[0].label


def test_build_steps_does_not_add_default_strategy_when_pull_args_already_pick_one(tmp_path) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")

    steps = build_steps("origin", no_default=True, pull_args=["--rebase"], cwd=checkout)

    assert steps[0].cmd == ["git", "pull", "--rebase"]
    assert "--no-rebase" not in steps[0].cmd


def test_build_steps_passes_through_extra_pull_args(tmp_path) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")

    steps = build_steps("origin", no_default=True, pull_args=["--ff-only"], cwd=checkout)

    assert steps[0].cmd == ["git", "pull", "--ff-only"]


# --- rerun_command ---------------------------------------------------------------


def test_rerun_command_minimal() -> None:
    assert rerun_command("origin", no_default=False, pull_args=[]) == "bdt pull"


def test_rerun_command_includes_non_default_remote_and_no_default_and_pull_args() -> None:
    cmd = rerun_command("upstream", no_default=True, pull_args=["--rebase", "--ff-only"])
    assert cmd == "bdt pull --remote upstream --no-default -- --rebase --ff-only"


# --- run() end to end ------------------------------------------------------------


def test_run_dry_run_does_not_pull_anything(tmp_path, capsys) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    commit_file(remote, "changed\n")

    run(dry_run=True, cwd=checkout)

    out = capsys.readouterr().out
    assert "would run: git pull" in out
    assert (checkout / "f.txt").read_text() == "base\n"


def test_run_prints_current_branch_header(tmp_path, capsys) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")

    run(dry_run=True, cwd=checkout)

    out = capsys.readouterr().out
    assert "bringing 'main' up to date from origin:" in out


def test_run_pulls_tracking_branch_and_main_and_default(tmp_path, capsys) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    commit_file(remote, "updated\n")

    run(cwd=checkout)

    assert (checkout / "f.txt").read_text() == "updated\n"
    out = capsys.readouterr().out
    assert "done" in out


def test_run_no_default_skips_default_branch_pull(tmp_path, capsys) -> None:
    remote = init_repo(tmp_path / "remote")
    _git(["checkout", "-q", "-b", "dev"], cwd=remote)
    checkout = clone(remote, tmp_path / "clone", checkout="dev")
    commit_file(remote, "on dev\n")

    run(no_default=True, cwd=checkout)

    out = capsys.readouterr().out
    assert "skip: origin's default branch" not in out  # no_default drops the step entirely
    assert "default branch" not in out


def test_run_exits_with_conflict_message_and_rerun_command_on_merge_conflict(tmp_path, capsys) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    commit_file(remote, "remote change\n")
    commit_file(checkout, "local conflicting change\n")

    with pytest.raises(SystemExit) as exc_info:
        run(cwd=checkout)

    message = str(exc_info.value)
    assert "merge conflict" in message
    assert "bdt pull" in message
    # left in a conflicted state for the user to resolve, not silently aborted
    unmerged = _git(["ls-files", "-u"], cwd=checkout)
    assert "f.txt" in unmerged.stdout


def test_run_reports_non_conflict_pull_failure(tmp_path, capsys) -> None:
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    _git(["remote", "set-url", "origin", str(tmp_path / "does-not-exist")], cwd=checkout)

    with pytest.raises(SystemExit) as exc_info:
        run(cwd=checkout)

    assert "failed" in str(exc_info.value)


def test_run_refuses_to_start_with_a_pre_existing_unrelated_conflict(tmp_path, capsys) -> None:
    """Regression test: without an up-front check, a conflict already sitting in the
    worktree from something unrelated (an earlier manual rebase, a previous unresolved
    `bdt pull`, ...) would make the *first* step's `git pull` fail and get mislabeled as
    "merge conflict while pulling <that step>" -- misattributing a pre-existing problem
    to whichever step happens to run first."""
    remote = init_repo(tmp_path / "remote")
    checkout = clone(remote, tmp_path / "clone")
    commit_file(remote, "remote change\n")
    commit_file(checkout, "local conflicting change\n")
    with pytest.raises(SystemExit):
        run(cwd=checkout)
    capsys.readouterr()  # discard output from the run() call above

    with pytest.raises(SystemExit) as exc_info:
        run(cwd=checkout)

    message = str(exc_info.value)
    assert "already has unresolved merge conflicts" in message
    out = capsys.readouterr().out
    assert "pulling" not in out  # refused before attempting any step
