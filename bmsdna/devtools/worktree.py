"""Create a git worktree for a new branch, mirroring the `just worktree` recipes."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


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

    subprocess.run(["git", "worktree", "add", str(path), "-b", name, base], cwd=root, check=True)

    if submodules and (root / ".gitmodules").exists():
        subprocess.run(["git", "submodule", "update", "--init"], cwd=path, check=True)

    if env_file is None:
        for candidate in (".local_env", ".env"):
            if (root / candidate).exists():
                env_file = candidate
                break

    if env_file and (root / env_file).exists():
        shutil.copy(root / env_file, path / ".env")

    if install_cmd:
        subprocess.run(install_cmd, cwd=path, check=True)

    print(f"worktree ready at {path}")
    return path
