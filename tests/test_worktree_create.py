"""`worktree.create`: where the env file and the install command go, at the repo root and in a monorepo sub-folder."""

import subprocess
from pathlib import Path

import pytest

from bmsdna.devtools import worktree


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, value in {"NAME": "t", "EMAIL": "t@t"}.items():
        monkeypatch.setenv(f"GIT_AUTHOR_{key}", value)
        monkeypatch.setenv(f"GIT_COMMITTER_{key}", value)
    work = tmp_path / "repo"
    (work / "app").mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    (work / "app" / "code.py").write_text("x")
    (work / ".gitignore").write_text(".worktrees/\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    (work / ".local_env").write_text("ROOT=1")
    (work / "app" / ".local_env").write_text("APP=1")
    return work


INSTALL = ["sh", "-c", "pwd -P > where.txt"]


def test_at_the_repo_root_env_and_install_use_the_worktree_root(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    worktree.create("feat", base="main", install_cmd=INSTALL)
    wt = repo / ".worktrees" / "feat"
    assert (wt / ".env").read_text() == "ROOT=1"
    assert (wt / "where.txt").read_text().strip() == str(wt.resolve())


def test_in_a_sub_folder_env_and_install_use_the_same_sub_folder_of_the_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo / "app")
    worktree.create("feat", base="main", install_cmd=INSTALL)
    wt = repo / "app" / ".worktrees" / "feat"
    assert (wt / "app" / "code.py").is_file()  # the worktree holds the whole repository
    assert (wt / "app" / ".env").read_text() == "APP=1"
    assert (wt / "app" / "where.txt").read_text().strip() == str((wt / "app").resolve())
    assert not (wt / ".env").exists() and not (wt / "where.txt").exists()


def test_the_ready_message_names_the_project_folder(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo / "app")
    worktree.create("feat", base="main")
    assert f"worktree ready at {repo / 'app' / '.worktrees' / 'feat' / 'app'}" in capsys.readouterr().out


def test_a_base_without_the_project_folder_falls_back_to_the_worktree_root(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = subprocess.run(
        ["git", "mktree"], cwd=repo, input="", capture_output=True, text=True, check=True
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "commit-tree", empty, "-m", "no app here"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    git(repo, "branch", "bare", commit)
    monkeypatch.chdir(repo / "app")
    worktree.create("feat", base="bare", install_cmd=INSTALL)
    wt = repo / "app" / ".worktrees" / "feat"
    assert (wt / "where.txt").is_file() and (wt / ".env").read_text() == "APP=1"
    assert "has no app/" in capsys.readouterr().err


def test_an_explicit_env_file_is_taken_from_the_sub_folder(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "app" / "custom_env").write_text("CUSTOM=1")
    monkeypatch.chdir(repo / "app")
    worktree.create("feat", base="main", env_file="custom_env")
    assert (repo / "app" / ".worktrees" / "feat" / "app" / ".env").read_text() == "CUSTOM=1"


def test_submodules_are_initialised_when_gitmodules_sits_at_the_repo_top(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / ".gitmodules").write_text("")
    git(repo, "add", ".gitmodules")
    git(repo, "commit", "-q", "-m", "gitmodules")
    calls: list[list[str]] = []
    real = worktree._run
    monkeypatch.setattr(worktree, "_run", lambda cmd, cwd: (calls.append(cmd), real(cmd, cwd))[1])
    monkeypatch.chdir(repo / "app")
    worktree.create("feat", base="main")
    assert ["git", "submodule", "update", "--init"] in calls


def test_outside_a_git_work_tree_nothing_is_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="not inside a git work tree"):
        worktree.create("feat", base="main")
    assert not (tmp_path / ".worktrees").exists()


def test_create_returns_the_folder_it_announces(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo / "app")
    assert worktree.create("feat", base="main") == repo / "app" / ".worktrees" / "feat" / "app"
