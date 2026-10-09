"""List the variable *names* defined in dotenv-style files, never their values.

Lets an agent (or human) find out which variables exist without `cat .env` /
`printenv` putting secrets into the transcript.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=")
# Never descended into: dependency/VCS/tooling trees that are huge and never hold project env files.
SKIP_DIRS = frozenset({"node_modules", ".git", ".venv", ".worktrees", ".claude"})

_EXPORT_RE = re.compile(r"^\s*export\s+[A-Za-z_]")


@dataclass
class EnvFile:
    path: Path
    keys: list[str] = field(default_factory=list)
    uses_export: bool = False  # bash syntax (`export VAR=VALUE`): meant to be sourced
    in_home: bool = False


def _is_env_name(name: str) -> bool:
    return name == ".env" or name.endswith(".env") or name.startswith(".env.")


def _walk_env_files(root: Path) -> list[Path]:
    """Env files under root, pruning SKIP_DIRS (and not following symlinked dirs) so big trees stay fast."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        base = Path(dirpath)
        found.extend(base / n for n in sorted(filenames) if _is_env_name(n) and (base / n).is_file())
    return found


def find_env_files(cwd: Path, home: Path | None = None) -> list[tuple[Path, bool]]:
    """`.env`, `*.env` and `.env.*` under cwd (recursive, skipping SKIP_DIRS) and directly in home.

    Returns (path, in_home). When cwd is home itself it is not walked recursively.
    """
    found: list[tuple[Path, bool]] = []
    seen: set[Path] = set()

    def add(p: Path, in_home: bool) -> None:
        real = p.resolve()
        if real not in seen:
            seen.add(real)
            found.append((p, in_home))

    if home is not None and cwd.resolve() == home.resolve():
        cwd_files = [p for p in sorted(cwd.iterdir()) if _is_env_name(p.name) and p.is_file()]
    else:
        cwd_files = _walk_env_files(cwd)
    for p in cwd_files:
        add(p, False)
    if home is not None:
        for p in sorted(home.iterdir()):
            if _is_env_name(p.name) and p.is_file():
                add(p, True)
    return found


def parse_env_file(path: Path, in_home: bool = False) -> EnvFile:
    res = EnvFile(path=path, in_home=in_home)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return res
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _KEY_RE.match(line)
        if m:
            if m.group(1) not in res.keys:
                res.keys.append(m.group(1))
            if _EXPORT_RE.match(line):
                res.uses_export = True
    return res


def get_keys(cwd: Path, home: Path | None = None, search: str | None = None) -> list[EnvFile]:
    """Env files with their key names; with `search`, only keys containing it (case-insensitive)."""
    out: list[EnvFile] = []
    for path, in_home in find_env_files(cwd, home):
        ef = parse_env_file(path, in_home)
        if search:
            ef.keys = [k for k in ef.keys if search.lower() in k.lower()]
            if not ef.keys:
                continue
        out.append(ef)
    return out


def _display(path: Path, cwd: Path | None, home: Path | None) -> Path:
    for base, prefix in ((cwd, None), (home, Path("~"))):
        if base is None:
            continue
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        return prefix / rel if prefix else rel
    return path


def format_keys(files: list[EnvFile], home: Path | None = None, cwd: Path | None = None) -> str:
    """Paths under cwd are shown relative to it, home files as ~/..., so the `source` hint works as printed."""
    lines: list[str] = []
    for ef in files:
        shown = _display(ef.path, None if ef.in_home else cwd, home if ef.in_home else None)
        lines.append(f"{shown}:")
        if ef.in_home or ef.uses_export:
            src = f"./{shown}" if not shown.is_absolute() and not str(shown).startswith("~") else shown
            lines.append(f"  # bash syntax, load with: source {src}")
        lines.extend(f"  {k}" for k in ef.keys)
    return "\n".join(lines)
