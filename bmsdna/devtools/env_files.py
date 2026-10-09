"""List the variable *names* defined in dotenv-style files, never their values.

Lets an agent (or human) find out which variables exist without `cat .env` /
`printenv` putting secrets into the transcript.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=")
_EXPORT_RE = re.compile(r"^\s*export\s+[A-Za-z_]")


@dataclass
class EnvFile:
    path: Path
    keys: list[str] = field(default_factory=list)
    uses_export: bool = False  # bash syntax (`export VAR=VALUE`): meant to be sourced
    in_home: bool = False


def _is_env_file(p: Path) -> bool:
    name = p.name
    return p.is_file() and (
        name == ".env" or name.endswith(".env") or name.startswith(".env.")
    )


def find_env_files(cwd: Path, home: Path | None = None) -> list[tuple[Path, bool]]:
    """`.env`, `*.env` and `.env.*` in cwd and in home (not recursive). Returns (path, in_home)."""
    found: list[tuple[Path, bool]] = []
    seen: set[Path] = set()
    for base, in_home in ((cwd, False), (home, True)):
        if base is None:
            continue
        for p in sorted(base.iterdir()):
            real = p.resolve()
            if _is_env_file(p) and real not in seen:
                seen.add(real)
                found.append((p, in_home))
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


def get_keys(
    cwd: Path, home: Path | None = None, search: str | None = None
) -> list[EnvFile]:
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


def format_keys(files: list[EnvFile], home: Path | None = None) -> str:
    lines: list[str] = []
    for ef in files:
        shown = ef.path
        if home is not None:
            try:
                shown = Path("~") / ef.path.relative_to(home)
            except ValueError:
                pass
        lines.append(f"{shown}:")
        if ef.in_home or ef.uses_export:
            lines.append(f"  # bash syntax, load with: source {shown}")
        lines.extend(f"  {k}" for k in ef.keys)
    return "\n".join(lines)
