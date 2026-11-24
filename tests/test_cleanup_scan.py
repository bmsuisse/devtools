import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bmsdna.devtools import cleanup_scan
from bmsdna.devtools.cli import app

runner = CliRunner()


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _fill(directory: Path, mb: int = 2) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "blob").write_bytes(b"0" * mb * 2**20)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    # don't ask the real uv/brew/podman where their caches are
    monkeypatch.setattr(cleanup_scan, "_dir_of", lambda tool, *args, fallback: fallback)
    monkeypatch.setattr(cleanup_scan, "_podman_content", lambda: "podman not reachable")
    return h


def test_scan_caches_lists_rust_and_dotnet_caches_biggest_first(home) -> None:
    _fill(home / ".nuget/packages", 3)
    _fill(home / ".cargo/registry", 2)
    _fill(home / ".rustup/toolchains/stable", 1)

    rows = cleanup_scan.scan_caches(home, min_mb=1)

    assert [r.id for r in rows] == ["nuget", "cargo", "rustup"]
    assert rows[0].clean == "dotnet nuget locals all --clear"
    assert rows[2].risk.startswith("medium")


def test_scan_caches_hides_rows_below_min_mb(home) -> None:
    _fill(home / ".nuget/packages", 1)
    assert cleanup_scan.scan_caches(home, min_mb=50) == []


def test_scan_caches_sccache_clean_command_names_the_real_path(home) -> None:
    _fill(home / ".cache/sccache", 2)

    (row,) = cleanup_scan.scan_caches(home, min_mb=1)

    assert row.id == "sccache"
    assert row.clean == f"sccache --stop-server; rm -rf {shlex.quote(str(home / '.cache/sccache'))}"


def test_scan_caches_lists_each_app_cache_as_its_own_row(home) -> None:
    _fill(home / "Library/Caches/SomeApp", 2)
    _fill(home / "Library/Caches/pip", 2)

    rows = cleanup_scan.scan_caches(home, min_mb=1)

    assert {r.id for r in rows} == {"appcache:SomeApp", "pip"}


def test_stale_envs_only_counts_target_and_bin_obj_next_to_their_manifest(tmp_path) -> None:
    _fill(tmp_path / "rust/target")
    (tmp_path / "rust/Cargo.toml").touch()
    _fill(tmp_path / "dotnet/bin")
    _fill(tmp_path / "dotnet/obj")
    (tmp_path / "dotnet/App.csproj").touch()
    _fill(tmp_path / "plain/target")
    _fill(tmp_path / "plain/bin")
    _fill(tmp_path / "web/node_modules")

    found = cleanup_scan.find_stale_envs(tmp_path, days=0, min_mb=1)

    assert {e.path.relative_to(tmp_path).as_posix() for e in found} == {
        "rust/target",
        "dotnet/bin",
        "dotnet/obj",
        "web/node_modules",
    }


def test_stale_envs_ignores_recently_touched_dirs(tmp_path) -> None:
    _fill(tmp_path / "web/node_modules")
    assert cleanup_scan.find_stale_envs(tmp_path, days=30, min_mb=1) == []


@pytest.fixture
def repo_with_worktrees(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(cleanup_scan, "active_cwds", list)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("init")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


def _add_worktree(repo: Path, name: str) -> Path:
    path = repo / ".worktrees" / name
    _git(["worktree", "add", "-q", str(path), "-b", name, "main"], repo)
    return path


def test_inspect_worktrees_flags_dirty_unpushed_and_recent(repo_with_worktrees) -> None:
    wt = _add_worktree(repo_with_worktrees, "feature")
    (wt / "new.txt").write_text("wip")

    (row,) = cleanup_scan.inspect_worktrees(repo_with_worktrees, idle_hours=24)

    assert row.branch == "feature"
    assert row.dirty
    assert row.unpushed == 1  # no remote at all: the init commit is on no remote branch
    assert row.verdict.startswith("review: dirty, 1 unpushed")


def test_inspect_worktrees_is_safe_when_clean_pushed_and_idle(repo_with_worktrees, tmp_path) -> None:
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], tmp_path)
    _git(["remote", "add", "origin", str(origin)], repo_with_worktrees)
    _git(["push", "-q", "origin", "main"], repo_with_worktrees)
    _add_worktree(repo_with_worktrees, "feature")

    (row,) = cleanup_scan.inspect_worktrees(repo_with_worktrees, idle_hours=0)

    assert row.verdict == "safe"


def test_inspect_worktrees_flags_a_process_inside(repo_with_worktrees, monkeypatch) -> None:
    wt = _add_worktree(repo_with_worktrees, "feature")
    monkeypatch.setattr(cleanup_scan, "active_cwds", lambda: [str(wt / "sub")])

    (row,) = cleanup_scan.inspect_worktrees(repo_with_worktrees, idle_hours=0)

    assert row.active
    assert "active process" in row.verdict


def test_inspect_worktrees_reports_a_missing_dir_as_prunable(repo_with_worktrees) -> None:
    wt = _add_worktree(repo_with_worktrees, "feature")
    shutil.rmtree(wt)

    (row,) = cleanup_scan.inspect_worktrees(repo_with_worktrees, idle_hours=0)

    assert row.verdict.startswith("prunable")


def test_cli_stale_envs_json(tmp_path) -> None:
    _fill(tmp_path / "web/node_modules")

    result = runner.invoke(app, ["cleanup", "stale-envs", str(tmp_path), "--days", "0", "--min-mb", "1", "--json"])

    assert result.exit_code == 0, result.output
    (entry,) = json.loads(result.output)
    assert entry["path"].endswith("web/node_modules")


def test_cli_caches_json_has_free_space_and_rows(home, monkeypatch) -> None:
    _fill(home / ".nuget/packages", 2)
    monkeypatch.setattr(Path, "home", lambda: home)

    result = runner.invoke(app, ["cleanup", "caches", "--min-mb", "1", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [r["id"] for r in payload["rows"]] == ["nuget"]
    assert payload["free_gb"] > 0


def test_cli_inspect_prints_one_line_per_worktree(repo_with_worktrees) -> None:
    _add_worktree(repo_with_worktrees, "feature")

    result = runner.invoke(app, ["cleanup", "inspect", str(repo_with_worktrees)])

    assert result.exit_code == 0, result.output
    assert "1 worktrees under" in result.output
    assert "repo:feature" in result.output


def test_stale_envs_counts_a_touched_top_level_entry_as_recent_use(tmp_path) -> None:
    _fill(tmp_path / "web/node_modules/pkg")
    old = time.time() - 90 * 86400
    env = tmp_path / "web/node_modules"
    os.utime(env, (old, old))  # the dir itself looks old ...
    assert cleanup_scan.find_stale_envs(tmp_path, days=30, min_mb=1) == []  # ... but pkg/ was just written
    os.utime(env / "pkg", (old, old))
    assert [e.path for e in cleanup_scan.find_stale_envs(tmp_path, days=30, min_mb=1)] == [env]


def test_cache_clean_commands_quote_paths_with_shell_metacharacters(home) -> None:
    evil = home / "Library/Caches/x'; touch pwned; #"
    _fill(evil, 2)

    (row,) = cleanup_scan.scan_caches(home, min_mb=1)

    assert row.clean == f"rm -rf {shlex.quote(str(evil))}"
    assert shlex.split(row.clean) == ["rm", "-rf", str(evil)]


def test_inspect_worktrees_needs_review_when_a_git_probe_fails(repo_with_worktrees, monkeypatch) -> None:
    _add_worktree(repo_with_worktrees, "feature")
    real = cleanup_scan._git_out
    monkeypatch.setattr(cleanup_scan, "_git_out", lambda path, *args: None if args[0] == "log" else real(path, *args))

    (row,) = cleanup_scan.inspect_worktrees(repo_with_worktrees, idle_hours=0)

    assert row.verdict.startswith("review: git check failed")


def test_inspect_worktrees_never_reads_unknown_processes_as_safe(repo_with_worktrees, monkeypatch) -> None:
    _add_worktree(repo_with_worktrees, "feature")
    monkeypatch.setattr(cleanup_scan, "active_cwds", lambda: None)

    (row,) = cleanup_scan.inspect_worktrees(repo_with_worktrees, idle_hours=0)

    assert not row.active
    assert "cannot tell if a process is inside" in row.verdict


def test_active_cwds_is_unknown_when_lsof_lists_nothing(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/lsof")
    monkeypatch.setattr(cleanup_scan, "_run_out", lambda cmd, timeout=30: "")
    monkeypatch.setattr(Path, "is_dir", lambda self: False)  # no /proc fallback either

    assert cleanup_scan.active_cwds() is None
