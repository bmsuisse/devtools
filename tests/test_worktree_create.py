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
