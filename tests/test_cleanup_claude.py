import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bmsdna.devtools import cleanup_claude, cleanup_scan
from bmsdna.devtools.cli import app

runner = CliRunner()


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _version_dir(claude: Path, plugin: str, version: str) -> Path:
    d = claude / "plugins/cache/market" / plugin / version
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("x")
    return d


@pytest.fixture
def claude(tmp_path, monkeypatch) -> Path:
    c = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(c))
    installed = _version_dir(c, "tools", "2.0")
    (c / "plugins/installed_plugins.json").write_text(
        json.dumps({"plugins": {"tools@market": [{"installPath": str(installed)}]}})
    )
    return c


def test_plugin_versions_marks_only_unreferenced_versions_stale(claude) -> None:
    _version_dir(claude, "tools", "1.0")

    (row,) = cleanup_claude.plugin_versions(claude)

    assert (row.plugin, row.version, row.verdict) == ("tools@market", "1.0", "stale")


def test_plugin_versions_keeps_a_version_a_live_process_registered(claude) -> None:
    old = _version_dir(claude, "tools", "1.0")
    (old / ".in_use").mkdir()
    (old / ".in_use" / str(os.getpid())).write_text("")
    (old / ".in_use" / str(_dead_pid())).write_text("")

    (row,) = cleanup_claude.plugin_versions(claude)

    assert row.live_pids == [str(os.getpid())]
    assert row.verdict.startswith("review")


def test_plugin_versions_reports_nothing_without_the_installed_index(claude) -> None:
    _version_dir(claude, "tools", "1.0")
    (claude / "plugins/installed_plugins.json").unlink()

    assert cleanup_claude.plugin_versions(claude) == []


def test_delete_stale_plugins_removes_stale_only_and_prunes_empty_plugin_dirs(claude) -> None:
    stale = _version_dir(claude, "tools", "1.0")
    live = _version_dir(claude, "other", "1.0")
    (live / ".in_use").mkdir()
    (live / ".in_use" / str(os.getpid())).write_text("")
    lone = _version_dir(claude, "gone", "0.1")

    assert cleanup_claude.delete_stale_plugins(claude, []) == 2

    assert not stale.exists()
    assert not lone.parent.exists()
    assert live.exists()
    assert (claude / "plugins/cache/market/tools/2.0").exists()


def test_delete_stale_plugins_honours_the_given_paths(claude) -> None:
    a = _version_dir(claude, "tools", "1.0")
    b = _version_dir(claude, "tools", "1.1")

    assert cleanup_claude.delete_stale_plugins(claude, [a]) == 1

    assert not a.exists()
    assert b.exists()


def test_pid_alive_on_windows_never_signals_the_process(monkeypatch) -> None:
    def fail(*_args) -> None:
        raise AssertionError("os.kill terminates the process on Windows")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "kill", fail)
    monkeypatch.setattr(cleanup_claude, "_pid_alive_windows", lambda pid: pid == 42)

    assert cleanup_claude.pid_alive(42)
    assert not cleanup_claude.pid_alive(43)


def test_pid_alive_posix(monkeypatch) -> None:
    assert cleanup_claude.pid_alive(os.getpid())
    assert not cleanup_claude.pid_alive(_dead_pid())


def test_pid_alive_posix_treats_other_errors_as_alive(monkeypatch) -> None:
    def deny(*_args) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", deny)

    assert cleanup_claude.pid_alive(1)


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "work/repo"
    r.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], r)
    _git(["config", "user.email", "t@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    (r / "README.md").write_text("init")
    _git(["add", "."], r)
    _git(["commit", "-q", "-m", "init"], r)
    return r


def _claude_worktree(repo: Path, name: str) -> Path:
    w = repo / ".claude/worktrees" / name
    w.mkdir(parents=True)
    (w / "file.txt").write_text("only copy")
    return w


def test_unregistered_worktrees_skip_those_git_knows(repo, tmp_path) -> None:
    known = repo / ".claude/worktrees/known"
    _git(["worktree", "add", "-q", str(known), "-b", "known", "main"], repo)
    _claude_worktree(repo, "gone")

    rows = cleanup_claude.unregistered_claude_worktrees(tmp_path / "work", 2, [])

    assert [(r.path.name, r.verdict) for r in rows] == [("gone", "orphan")]


def test_unregistered_worktrees_review_when_a_process_is_inside(repo, tmp_path) -> None:
    w = _claude_worktree(repo, "busy")

    (row,) = cleanup_claude.unregistered_claude_worktrees(tmp_path / "work", 2, [str(w / "sub")])

    assert row.verdict == "review: active process inside"


def test_unregistered_worktrees_are_never_orphans_when_processes_cannot_be_listed(repo, tmp_path) -> None:
    _claude_worktree(repo, "unknown")

    (row,) = cleanup_claude.unregistered_claude_worktrees(tmp_path / "work", 2, None)

    assert row.verdict.startswith("review: cannot tell")


def test_unregistered_worktrees_review_a_pointer_to_an_existing_gitdir(repo, tmp_path) -> None:
    w = _claude_worktree(repo, "other")
    (w / ".git").write_text(f"gitdir: {repo / '.git'}")

    (row,) = cleanup_claude.unregistered_claude_worktrees(tmp_path / "work", 2, [])

    assert row.verdict == "review: owned by another clone"


def test_cli_claude_home_json(claude, repo, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cleanup_scan, "active_cwds", list)
    _version_dir(claude, "tools", "1.0")
    _claude_worktree(repo, "gone")

    result = runner.invoke(app, ["cleanup", "claude-home", str(tmp_path / "work"), "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [p["verdict"] for p in data["plugins"]] == ["stale"]
    assert [w["verdict"] for w in data["worktrees"]] == ["orphan"]
    assert data["process_check_available"] is True


def test_cli_claude_home_says_when_processes_cannot_be_listed(claude, repo, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cleanup_scan, "active_cwds", lambda: None)
    _claude_worktree(repo, "unknown")

    result = runner.invoke(app, ["cleanup", "claude-home", str(tmp_path / "work")])

    assert result.exit_code == 0, result.output
    assert "cannot be listed" in result.output
    assert "0 orphan" in result.output


def test_cli_claude_plugins_is_a_dry_run_without_yes(claude) -> None:
    stale = _version_dir(claude, "tools", "1.0")

    result = runner.invoke(app, ["cleanup", "claude-plugins"])

    assert result.exit_code == 0, result.output
    assert "would remove tools@market 1.0" in result.output
    assert stale.exists()


def test_cli_claude_plugins_yes_removes_stale_versions(claude) -> None:
    stale = _version_dir(claude, "tools", "1.0")

    result = runner.invoke(app, ["cleanup", "claude-plugins", "--yes"])

    assert result.exit_code == 0, result.output
    assert "removed 1 stale plugin versions" in result.output
    assert not stale.exists()
