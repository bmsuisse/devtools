"""`bdt find-repo <name>`: locate a repo by name, locally first and then in
an Azure DevOps org, without needing the full `ALL_REPOS.md` org sync that
the `cross-repo-discovery` skill (bmsuisse/skills) used to require.

Env vars mirror that skill's so switching over needs no reconfiguration:
- `AZDO_WORK_DIR` / `BMS_WORK_DIR`: local clone root to search (and to clone
  a remote match into). Falls back to `C:/Projects` on Windows, `~/projects`
  elsewhere.
- `AZDO_ORG` / `BMS_ORG`: Azure DevOps org to search when no local match is
  found.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .cli_tools import require_az
from .worktree import find_repos


def work_dir() -> Path:
    env = os.environ.get("AZDO_WORK_DIR") or os.environ.get("BMS_WORK_DIR")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path("C:/Projects")
    return Path("~/projects").expanduser()


def resolve_org(cli_org: str | None) -> str | None:
    if cli_org:
        return cli_org
    return os.environ.get("AZDO_ORG") or os.environ.get("BMS_ORG")


def find_local(name: str, root: Path) -> list[Path]:
    """Repos under `root` whose folder name matches `name`, case-insensitively.

    Prefers exact matches; only falls back to substring matches (also
    case-insensitive) when no exact match exists, so a short/common name
    doesn't drown in unrelated results.
    """
    repos = find_repos(root)
    exact = [r for r in repos if r.name.lower() == name.lower()]
    if exact:
        return sorted(exact)
    return sorted(r for r in repos if name.lower() in r.name.lower())


@dataclass(frozen=True)
class RemoteRepo:
    project: str
    name: str
    remote_url: str


def _az(az: str, *args: str) -> str:
    result = subprocess.run([az, *args, "-o", "json"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"az {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def find_remote(az: str, org: str, name: str) -> list[RemoteRepo]:
    """Search every project in `org` for a repo matching `name`, the same
    exact-first/substring-fallback rule as `find_local`.

    Azure DevOps has no "search repos by name across the whole org" API, so
    (as the old skill's script did) this lists every project and then every
    project's repos -- there's no cheaper way to do it.
    """
    projects = json.loads(_az(az, "devops", "project", "list", "--org", f"https://dev.azure.com/{org}"))["value"]

    all_repos: list[RemoteRepo] = []
    for project in projects:
        repos = json.loads(_az(az, "repos", "list", "--org", f"https://dev.azure.com/{org}", "--project", project["name"]))
        for r in repos:
            all_repos.append(RemoteRepo(project["name"], r["name"], r["remoteUrl"]))

    exact = [r for r in all_repos if r.name.lower() == name.lower()]
    if exact:
        return exact
    return [r for r in all_repos if name.lower() in r.name.lower()]


def clone(remote_url: str, dest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "clone", remote_url, str(dest)], check=False)


def run(name: str, *, root: Path | None, org: str | None, yes: bool) -> None:
    """`bdt find-repo <name>`: search `root` (default `work_dir()`) for a local
    match, then -- if there isn't one -- `org`'s Azure DevOps repos, offering
    to clone a single remote-only match into `root`.
    """
    search_root = root or work_dir()
    if not search_root.is_dir():
        sys.exit(
            f"Local work dir {search_root} doesn't exist. Set AZDO_WORK_DIR (or BMS_WORK_DIR) to "
            "where your repos are cloned, or pass --root."
        )

    local_matches = find_local(name, search_root)
    if local_matches:
        for path in local_matches:
            print(path)
        return

    if not org:
        sys.exit(
            f"No local repo matching '{name}' found under {search_root}. "
            "Set AZDO_ORG (or BMS_ORG) to also search Azure DevOps, or pass --org."
        )

    az = require_az()
    remote_matches = find_remote(az, org, name)
    if not remote_matches:
        sys.exit(f"No repo matching '{name}' found locally under {search_root} or in the '{org}' Azure DevOps org.")

    for r in remote_matches:
        print(f"{r.project}/{r.name}  {r.remote_url}")

    if len(remote_matches) > 1:
        sys.exit("\nMultiple matches -- narrow the name or clone manually.")

    match = remote_matches[0]
    dest = search_root / match.name
    if not yes:
        try:
            answer = input(f"\nNot found locally. Clone into {dest}? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Not cloned.")
            return

    result = clone(match.remote_url, dest)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
