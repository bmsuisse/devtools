"""Create a git worktree for a new branch, mirroring the `just worktree` recipes."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], cwd: Path) -> None:
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
    except FileNotFoundError:
        sys.exit(f"'{cmd[0]}' is required for this command but wasn't found on PATH.")


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
