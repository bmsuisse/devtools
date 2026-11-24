"""Read-only disk-usage reports behind `bdt cleanup caches`, `bdt cleanup stale-envs` and `bdt cleanup inspect`.

Nothing here deletes anything: each row carries the command that would clean it, for the caller to run after the
user picked it (see the dev-cleanup skill). Sizes come from `du -sk` where available (it counts hard links once,
which matters for the uv/pnpm stores), else from a plain directory walk.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from bmsdna.devtools import worktree as worktree_mod

# `target`, `bin` and `obj` are common names: they only count next to the manifest that makes them build output.
_ENV_DIR_NAMES = frozenset({"node_modules", ".venv", "target", "bin", "obj", ".next", ".turbo"})
_DOTNET_PROJECTS = (".csproj", ".fsproj", ".vbproj")


def _run_out(cmd: list[str], timeout: float = 30) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout.strip()
    except OSError, subprocess.TimeoutExpired:
        return ""


def _walk_size_mb(path: Path) -> float:
    total, seen = 0, set()
    for dp, _, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                st = Path(dp, f).lstat()
                if st.st_nlink > 1:
                    if (st.st_dev, st.st_ino) in seen:
                        continue
                    seen.add((st.st_dev, st.st_ino))
                total += st.st_size
    return total / 2**20


def size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    if shutil.which("du"):
        out = _run_out(["du", "-sk", str(path)], timeout=300).split()
        if out and out[0].isdigit():
            return int(out[0]) / 1024
    return _walk_size_mb(path)


# --- caches ---------------------------------------------------------------


@dataclass
class CacheRow:
    id: str
    label: str
    path: Path
    clean: str
    risk: str
    size_mb: float = 0.0


def _first_existing(*paths: Path) -> Path:
    return next((p for p in paths if p.exists()), paths[0])


def _rm(path: Path) -> str:
    return f"rm -rf {shlex.quote(str(path))}"


def _podman_content() -> str:
    """`podman system df` shows real content; the folder size is the VM's raw disk, which never shrinks by itself."""
    out = _run_out(["podman", "system", "df", "--format", "{{.Type}}: {{.Size}} ({{.Reclaimable}} reclaimable)"])
    return (
        " | ".join(out.splitlines()) if out else "podman not reachable (start `podman machine`) - real content unknown"
    )


def _dir_of(tool: str, *args: str, fallback: Path) -> Path:
    out = _run_out([tool, *args]) if shutil.which(tool) else ""
    return Path(out.splitlines()[-1]) if out else fallback


def cache_specs(home: Path) -> list[CacheRow]:
    lib = home / "Library"
    sccache = _first_existing(lib / "Caches/Mozilla.sccache", home / ".cache/sccache")
    pnpm = _first_existing(lib / "pnpm/store", home / ".local/share/pnpm/store")
    pip = _first_existing(lib / "Caches/pip", home / ".cache/pip")
    playwright = _first_existing(lib / "Caches/ms-playwright", home / ".cache/ms-playwright")
    huggingface = home / ".cache/huggingface"
    return [
        CacheRow(
            "podman",
            "Podman VM disk file; real content inside: " + _podman_content(),
            home / ".local/share/containers",
            "podman machine ssh -- sudo fstrim -av  (shrinks the VM disk file), then podman system prune -a -f; "
            "--volumes only if user agrees",
            "medium: prune -a removes every image not used by a running container; folder size is the VM disk, "
            "real gain is often much smaller",
        ),
        CacheRow(
            "docker", "Docker Desktop data", lib / "Containers/com.docker.docker", "docker system prune -a -f", "medium"
        ),
        CacheRow(
            "uv",
            "uv cache",
            _dir_of("uv", "cache", "dir", fallback=home / ".cache/uv"),
            "uv cache clean",
            "low: re-downloaded on next sync",
        ),
        CacheRow("npm", "npm cache", home / ".npm", "npm cache clean --force", "low"),
        CacheRow("bun", "bun install cache", home / ".bun/install/cache", "bun pm cache rm", "low"),
        CacheRow("pnpm", "pnpm store", pnpm, "pnpm store prune", "low"),
        CacheRow("pip", "pip cache", pip, "pip cache purge", "low"),
        CacheRow(
            "cargo",
            "cargo registry + git checkouts",
            home / ".cargo/registry",
            "rm -rf ~/.cargo/registry/cache ~/.cargo/git/checkouts",
            "low: re-downloaded on build",
        ),
        CacheRow(
            "sccache",
            "sccache compile cache",
            sccache,
            f"sccache --stop-server; {_rm(sccache)}",
            "low: slower next build",
        ),
        CacheRow(
            "rustup",
            "rustup toolchains (`rustup toolchain list`)",
            home / ".rustup/toolchains",
            "rustup toolchain uninstall <name>   # list first, keep the default",
            "medium: a repo pinning that toolchain re-installs it",
        ),
        CacheRow(
            "nuget",
            "NuGet package cache",
            home / ".nuget/packages",
            "dotnet nuget locals all --clear",
            "low: re-downloaded on restore",
        ),
        CacheRow("gradle", "gradle caches", home / ".gradle/caches", "rm -rf ~/.gradle/caches", "low: slow next build"),
        CacheRow(
            "huggingface",
            "HuggingFace model cache",
            huggingface,
            f"rm -rf {shlex.quote(str(huggingface))}/<model>   # list models first, they can be many GB",
            "medium: models must be re-downloaded",
        ),
        CacheRow(
            "playwright",
            "Playwright browsers",
            playwright,
            _rm(playwright),
            "low: `playwright install` restores",
        ),
        CacheRow(
            "homebrew",
            "Homebrew downloads",
            _dir_of("brew", "--cache", fallback=lib / "Caches/Homebrew"),
            "brew cleanup --prune=all",
            "low",
        ),
        CacheRow(
            "xcode-derived",
            "Xcode DerivedData",
            lib / "Developer/Xcode/DerivedData",
            _rm(lib / "Developer/Xcode/DerivedData"),
            "low: rebuilt",
        ),
        CacheRow(
            "xcode-sim",
            "Xcode simulators (unavailable)",
            lib / "Developer/CoreSimulator",
            "xcrun simctl delete unavailable",
            "low",
        ),
        CacheRow("trash", "Trash", home / ".Trash", "empty Trash (user does it in Finder)", "check first"),
        CacheRow("logs", "~/Library/Logs", lib / "Logs", "rm -rf ~/Library/Logs/*", "low"),
    ]


def _app_caches(home: Path, known: set[Path]) -> list[CacheRow]:
    """Each app cache under ~/Library/Caches as its own row, so the user can pick them one by one."""
    root = home / "Library/Caches"
    if not root.is_dir():
        return []
    return [
        CacheRow(f"appcache:{d.name}", f"App cache {d.name}", d, _rm(d), "low: app rebuilds it")
        for d in sorted(root.iterdir())
        if d.is_dir() and d not in known
    ]


def scan_caches(home: Path, min_mb: float) -> list[CacheRow]:
    rows = cache_specs(home)
    rows += _app_caches(home, {r.path for r in rows})
    with ThreadPoolExecutor(8) as ex:
        for row, mb in zip(rows, ex.map(lambda r: size_mb(r.path), rows), strict=True):
            row.size_mb = mb
    return sorted((r for r in rows if r.size_mb >= min_mb), key=lambda r: -r.size_mb)


# --- stale build / dependency dirs ---------------------------------------


@dataclass(frozen=True)
class StaleEnv:
    path: Path
    size_mb: float


def _is_env_dir(name: str, parent_files: list[str]) -> bool:
    if name == "target":
        return "Cargo.toml" in parent_files
    if name in {"bin", "obj"}:
        return any(f.endswith(_DOTNET_PROJECTS) for f in parent_files)
    return name in _ENV_DIR_NAMES


def _last_touched(env_dir: Path) -> float:
    """Newest mtime of the dir and its top-level entries: a dir's own mtime only moves when entries are added or
    removed, so a `uv sync`/`cargo build`/`npm install` into an existing env would otherwise look untouched."""
    newest = env_dir.stat().st_mtime
    with contextlib.suppress(OSError):
        for entry in env_dir.iterdir():
            with contextlib.suppress(OSError):
                newest = max(newest, entry.stat().st_mtime)
    return newest


def find_stale_envs(root: Path, days: float, min_mb: float) -> list[StaleEnv]:
    """node_modules, .venv, Rust `target`, .NET `bin`/`obj` (and .next/.turbo) untouched for `days` days (age: see
    `_last_touched`).
    They all regenerate (`bun install`, `uv sync`, `cargo build`, `dotnet build`)."""
    cutoff = time.time() - days * 86400
    candidates: list[Path] = []
    for dp, dirs, files in os.walk(root):
        keep = []
        for d in dirs:
            if _is_env_dir(d, files):
                if _last_touched(Path(dp, d)) < cutoff:
                    candidates.append(Path(dp, d))
            elif d != ".git":
                keep.append(d)
        dirs[:] = keep  # never descend into the env dirs themselves
    with ThreadPoolExecutor(8) as ex:
        found = [StaleEnv(p, mb) for p, mb in zip(candidates, ex.map(size_mb, candidates), strict=True)]
    return sorted((e for e in found if e.size_mb >= min_mb), key=lambda e: -e.size_mb)


# --- worktrees ------------------------------------------------------------


@dataclass
class WorktreeRow:
    repo: Path
    path: Path
    branch: str
    locked: bool
    size_mb: float = 0.0
    dirty: bool = False
    unpushed: int = 0
    active: bool = False
    idle_h: float = 0.0
    verdict: str = ""


def _git_out(path: Path, *args: str) -> str | None:
    """Stdout of a git command, or None when it failed: a failed probe must not read as "clean / nothing unpushed"."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, timeout=120, check=False
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 else None


def active_cwds() -> list[str] | None:
    """Working dirs of all running processes (a per-worktree `lsof +D` is far too slow), or None when this
    platform offers no way to list them (Windows): callers must not read that as "nothing is running"."""
    if shutil.which("lsof"):
        return [line[1:] for line in _run_out(["lsof", "-w", "-d", "cwd", "-Fn"], 120).splitlines() if line[:2] == "n/"]
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    cwds = []
    for p in proc.glob("[0-9]*/cwd"):
        with contextlib.suppress(OSError):
            cwds.append(str(p.readlink()))
    return cwds


def _inspect(wt: WorktreeRow, cwds: list[str] | None, idle_hours: float) -> WorktreeRow:
    if not wt.path.is_dir():
        wt.verdict = "prunable (dir missing): git worktree prune"
        return wt
    wt.size_mb = round(size_mb(wt.path))
    status = _git_out(wt.path, "status", "--porcelain")
    wt.dirty = bool((status or "").strip())
    # commits reachable from HEAD but from no remote branch: lost if the worktree+branch were deleted
    unpushed = _git_out(wt.path, "log", "--oneline", "HEAD", "--not", "--remotes")
    wt.unpushed = len((unpushed or "").splitlines())
    p = str(wt.path)
    wt.active = any(c == p or c.startswith(p + os.sep) for c in cwds or [])
    last_commit = _git_out(wt.path, "log", "-1", "--format=%ct")
    touched = [wt.path.stat().st_mtime, int((last_commit or "").strip() or 0)]
    top_level = [f for f in wt.path.iterdir() if f.name != "node_modules"][:200]
    for f in top_level:
        with contextlib.suppress(OSError):
            touched.append(f.stat().st_mtime)
    wt.idle_h = round((time.time() - max(touched)) / 3600)
    why = [
        w
        for w, bad in [
            ("git check failed", None in (status, unpushed, last_commit)),
            ("dirty", wt.dirty),
            (f"{wt.unpushed} unpushed", wt.unpushed),
            ("locked", wt.locked),
            ("active process", wt.active),
            ("cannot tell if a process is inside (no lsof or /proc)", cwds is None),
            (f"touched {wt.idle_h:.0f}h ago", wt.idle_h < idle_hours),
        ]
        if bad
    ]
    wt.verdict = "safe" if not why else "review: " + ", ".join(why)
    return wt


def inspect_worktrees(root: Path, idle_hours: float) -> list[WorktreeRow]:
    """Every linked worktree of every repo under `root`. Verdict `safe` = clean, nothing unpushed, not locked,
    no process inside, idle for at least `idle_hours`."""
    rows = [
        WorktreeRow(repo, Path(e["path"]), e.get("branch", "(detached)"), bool(e.get("locked")))
        for repo in sorted(worktree_mod.find_repos(root))
        for e in worktree_mod._parse_worktree_list(repo)[1:]
        if not e.get("bare")
    ]
    cwds = active_cwds()
    with ThreadPoolExecutor(8) as ex:
        done = list(ex.map(lambda r: _inspect(r, cwds, idle_hours), rows))
    return sorted(done, key=lambda r: -r.size_mb)
