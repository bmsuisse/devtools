"""Claude Code leftovers behind `bdt cleanup claude-home` and `bdt cleanup claude-plugins`: old plugin versions in
`~/.claude/plugins/cache` and stale `<repo>/.claude/worktrees/*` dirs.

Claude Code installs every plugin update into a new `cache/<marketplace>/<plugin>/<version>/` dir and never removes
the old ones. A version is `stale` when no entry in installed_plugins.json points at it and no live process is
registered in its `.in_use/<pid>` files (`.orphaned_at` is Claude's own marker, shown as the age).

Worktrees that git still knows are covered by `bdt cleanup inspect|worktrees|worktree`. `unregistered_claude_worktrees`
lists only dirs under `.claude/worktrees` that git does NOT know (`orphan`: gitdir gone), which those cannot see.
Only `delete_stale_plugins` deletes anything.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from bmsdna.devtools import cleanup_scan
from bmsdna.devtools import worktree as worktree_mod

_WIN_ERROR_ACCESS_DENIED = 5
_WIN_ERROR_INVALID_PARAMETER = 87  # OpenProcess for a pid that does not exist
_WIN_STILL_ACTIVE = 259
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def _pid_alive_windows(pid: int) -> bool:
    """Probe without signalling: on Windows `os.kill(pid, 0)` calls TerminateProcess and kills the process."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() != _WIN_ERROR_INVALID_PARAMETER  # access denied etc.: cannot tell, assume alive
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _WIN_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid runs; "cannot tell" counts as alive, so nothing live is ever cleaned up."""
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists, owned by someone else
        return True
    return True


@dataclass
class PluginVersion:
    path: Path
    plugin: str
    version: str
    orphaned_days: int | None
    live_pids: list[str]
    size_mb: float
    verdict: str


@dataclass
class ClaudeWorktree:
    path: Path
    repo: Path
    why: str
    size_mb: float
    idle_days: int
    verdict: str


@dataclass
class ClaudeHomeReport:
    plugins: list[PluginVersion] = field(default_factory=list)
    worktrees: list[ClaudeWorktree] = field(default_factory=list)
    process_check_available: bool = True


def plugin_versions(claude: Path, with_size: bool = True) -> list[PluginVersion]:
    """Every cached plugin version that is not installed anywhere, with a verdict (re-read each call)."""
    plugins = claude / "plugins"
    try:
        installed = json.loads((plugins / "installed_plugins.json").read_text(encoding="utf-8"))["plugins"]
    except OSError, ValueError, KeyError:
        return []  # without the index we cannot tell what is installed: report nothing rather than guess
    keep = {Path(e["installPath"]).resolve() for entries in installed.values() for e in entries}
    rows = []
    for ver in (plugins / "cache").glob("*/*/*"):
        if not ver.is_dir() or ver.resolve() in keep:
            continue
        in_use = ver / ".in_use"
        live = (
            [p.name for p in in_use.iterdir() if p.name.isdigit() and pid_alive(int(p.name))] if in_use.is_dir() else []
        )
        marker = ver / ".orphaned_at"
        age_d = None
        with contextlib.suppress(OSError, ValueError):
            if marker.is_file():
                age_d = (time.time() - int(marker.read_text(encoding="utf-8").strip() or 0) / 1000) / 86400
        rows.append(
            PluginVersion(
                path=ver,
                plugin=f"{ver.parent.name}@{ver.parent.parent.name}",
                version=ver.name,
                orphaned_days=round(age_d) if age_d is not None else None,
                live_pids=live,
                size_mb=round(cleanup_scan.size_mb(ver)) if with_size else 0,
                verdict="review: used by a live process" if live else "stale",
            )
        )
    return sorted(rows, key=lambda r: -r.size_mb)


def delete_stale_plugins(claude: Path, only: list[Path]) -> int:
    """Re-checks installed/live state right now (no size scan in between), then removes the stale versions (only
    the `only` paths if given). Returns how many versions were removed."""
    removed, wanted = 0, {p.resolve() for p in only}
    for row in plugin_versions(claude, with_size=False):
        if row.verdict == "stale" and (not wanted or row.path.resolve() in wanted):
            shutil.rmtree(row.path, ignore_errors=True)
            removed += 1
    for plugin_dir in sorted((claude / "plugins" / "cache").glob("*/*")):  # plugin dirs left with no version at all
        if plugin_dir.is_dir() and not any(plugin_dir.iterdir()):
            plugin_dir.rmdir()
    return removed


def _registered_worktrees(repo: Path) -> set[Path]:
    return {Path(e["path"]).resolve() for e in worktree_mod._parse_worktree_list(repo)}


def _newest_mtime(w: Path) -> float:
    """Newest mtime among the dir and its entries two levels deep (node_modules/.git skipped): a cheap 'last used'."""
    skip = {"node_modules", ".git", ".venv"}
    times = [w.stat().st_mtime]
    for c in w.iterdir():
        if c.name in skip:
            continue
        with contextlib.suppress(OSError):
            times.append(c.stat().st_mtime)
            if c.is_dir() and not c.is_symlink():
                times += [g.stat().st_mtime for g in list(c.iterdir())[:200]]
    return max(times)


def _verdict(w: Path, cwds: list[str] | None) -> tuple[str, str]:
    gitfile = w / ".git"
    ptr = gitfile.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip() if gitfile.is_file() else ""
    missing = "gitdir missing" if ptr else "no .git file"
    if gitfile.is_dir():
        return "review: standalone clone", ".git is a directory"
    if ptr and (w / ptr).exists():  # relative pointers resolve against the worktree; absolute ones ignore `w`
        return "review: owned by another clone", f"gitdir {ptr}"
    if cwds is None:
        return "review: cannot tell if a process is inside (no lsof or /proc)", missing
    if any(c == str(w) or c.startswith(str(w) + os.sep) for c in cwds):
        return "review: active process inside", missing
    return "orphan", missing


def unregistered_claude_worktrees(root: Path, depth: int, cwds: list[str] | None) -> list[ClaudeWorktree]:
    """`cwds` as from `cleanup_scan.active_cwds()`; None (unknown) keeps dirs out of the `orphan` verdict."""
    rows: list[ClaudeWorktree] = []
    stack = [(root, 0)]
    while stack:
        d, n = stack.pop()
        if (d / ".git").is_dir():
            wt_dir = d / ".claude" / "worktrees"
            known = _registered_worktrees(d) if wt_dir.is_dir() else set()
            for w in sorted(wt_dir.iterdir()) if wt_dir.is_dir() else []:
                if not w.is_dir() or w.resolve() in known:
                    continue
                verdict, why = _verdict(w, cwds)
                rows.append(
                    ClaudeWorktree(
                        path=w,
                        repo=d,
                        why=why,
                        size_mb=round(cleanup_scan.size_mb(w)),
                        idle_days=round((time.time() - _newest_mtime(w)) / 86400),
                        verdict=verdict,
                    )
                )
            continue
        if n < depth:
            with contextlib.suppress(PermissionError):
                stack += [(c, n + 1) for c in d.iterdir() if c.is_dir() and not c.name.startswith(".")]
    return sorted(rows, key=lambda r: -r.size_mb)


def scan(claude: Path, root: Path, depth: int) -> ClaudeHomeReport:
    cwds = cleanup_scan.active_cwds()
    return ClaudeHomeReport(
        plugins=plugin_versions(claude),
        worktrees=unregistered_claude_worktrees(root, depth, cwds),
        process_check_available=cwds is not None,
    )
