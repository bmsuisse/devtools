"""`bdt find-repo <name>`: locate a repo by name, locally first and then in
an Azure DevOps org and/or a GitHub org, without needing the full
`ALL_REPOS.md` org sync that the `cross-repo-discovery` skill
(bmsuisse/skills) used to require.

Env vars mirror that skill's so switching over needs no reconfiguration:
- `AZDO_WORK_DIR` / `BMS_WORK_DIR`: local clone root to search (and to clone
  a remote match into). Falls back to `C:/Projects` on Windows, `~/projects`
  elsewhere.
- `AZDO_ORG` / `BMS_ORG`: Azure DevOps org to search when no local match is
  found.
- `GITHUB_ORG` / `BMS_GITHUB_ORG`: GitHub org to search when no local match
  is found. A match is cloned into `root/github/<repo>` -- GitHub repos
  aren't split into ADO-style projects, so by convention they all land in
  one `github` subfolder regardless of which org they came from.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .ado_auth import auth_header
from .cli_tools import require_az, require_gh
from .worktree import find_repos

T = TypeVar("T")


def work_dir() -> Path:
    env = os.environ.get("AZDO_WORK_DIR") or os.environ.get("BMS_WORK_DIR")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path("C:/Projects")
    return Path("~/projects").expanduser()


def _prefer_exact(items: list[T], name: str, key: Callable[[T], str]) -> list[T]:
    """Exact (case-insensitive) matches on `key(item)` if there are any, else
    substring matches -- shared by `find_local`/`find_remote`/`find_github`
    (and, cross-source, by `run`) so a short/common name doesn't drown in
    unrelated results.
    """
    exact = [i for i in items if key(i).lower() == name.lower()]
    if exact:
        return exact
    return [i for i in items if name.lower() in key(i).lower()]


def find_local(name: str, root: Path) -> list[Path]:
    """Repos under `root` whose folder name matches `name` -- see `_prefer_exact`."""
    return sorted(_prefer_exact(find_repos(root), name, key=lambda r: r.name))


@dataclass(frozen=True)
class RemoteRepo:
    project: str
    name: str
    remote_url: str
    source: str = "ado"


def _az(az: str, *args: str) -> tuple[bool, str]:
    """Non-fatal `az ... -o json` run: (ok, stdout-on-success | stderr-on-failure).

    Deliberately not one of the existing `run_az`/`_run_gh_json` helpers
    elsewhere in this codebase -- those either exit on any failure or merge
    stdout+stderr into one string, neither of which works for `find_remote`'s
    per-project fan-out below, where one project failing (e.g. no access)
    must not abort the whole org search, and stdout has to stay pure JSON.
    """
    result = subprocess.run([az, *args, "-o", "json"], capture_output=True, text=True, check=False)
    return result.returncode == 0, result.stdout if result.returncode == 0 else result.stderr


def find_remote(az: str, org: str, name: str) -> list[RemoteRepo]:
    """Search every project in `org` for a repo matching `name` -- see `_prefer_exact`.

    Azure DevOps has no "search repos by name across the whole org" API, so
    (as the old skill's script did) this lists every project and then every
    project's repos -- there's no cheaper way to do it. The per-project calls
    run concurrently (each is just an `az` subprocess waiting on network I/O)
    since with dozens of projects a serial fan-out is the dominant cost.
    """
    ok, out = _az(az, "devops", "project", "list", "--org", f"https://dev.azure.com/{org}")
    if not ok:
        sys.exit(f"az devops project list failed (try `az login`?):\n{out.strip()}")
    projects = json.loads(out)["value"]

    def repos_for(project: dict) -> list[RemoteRepo]:
        ok, out = _az(az, "repos", "list", "--org", f"https://dev.azure.com/{org}", "--project", project["name"])
        if not ok:
            print(f"warning: couldn't list repos for project '{project['name']}': {out.strip()}", file=sys.stderr)
            return []
        return [RemoteRepo(project["name"], r["name"], r["remoteUrl"]) for r in json.loads(out)]

    all_repos: list[RemoteRepo] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for repos in pool.map(repos_for, projects):
            all_repos.extend(repos)

    return _prefer_exact(all_repos, name, key=lambda r: r.name)


def find_github(gh: str, org: str, name: str) -> list[RemoteRepo]:
    """Search `org`'s GitHub repos for a match -- see `_prefer_exact`.

    Unlike Azure DevOps, GitHub has a single flat list of repos per org/user
    (no per-project split), so `gh repo list` covers the whole org in one call.
    """
    result = subprocess.run(
        [gh, "repo", "list", org, "--json", "name,url", "--limit", "1000"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        sys.exit(f"gh repo list failed (try `gh auth login`?):\n{result.stderr.strip()}")

    all_repos = [RemoteRepo(org, r["name"], r["url"], source="github") for r in json.loads(result.stdout)]
    return _prefer_exact(all_repos, name, key=lambda r: r.name)


def clone(remote_url: str, dest: Path, auth: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Clone `remote_url` (an HTTPS Azure DevOps URL) into `dest`.

    `auth` (from `ado_auth.auth_header`) is passed to git as an HTTP
    Authorization header via `GIT_CONFIG_*` env vars rather than a
    `-c http.extraheader=...` CLI argument -- the latter would leak the PAT
    to anyone able to see this process's argv (`ps aux`, /proc/<pid>/cmdline).
    """
    env = os.environ.copy()
    if auth:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: {auth['Authorization']}"
    try:
        return subprocess.run(["git", "clone", remote_url, str(dest)], env=env, check=False)
    except FileNotFoundError:
        sys.exit("'git' is required for this command but wasn't found on PATH.")


def clone_github(gh: str, full_name: str, dest: Path) -> subprocess.CompletedProcess:
    """Clone a GitHub repo via `gh repo clone`, so it authenticates the same way
    `gh repo list` already did.

    Deliberately not `clone()` + a hand-built header: `auth_header` (ADO's Basic-PAT-or-
    `az`-Bearer-token helper) must never be sent to github.com, and `gh` already knows how
    to authenticate its own git operations (private repos included).
    """
    try:
        return subprocess.run([gh, "repo", "clone", full_name, str(dest)], check=False)
    except FileNotFoundError:
        sys.exit("'gh' is required for this command but wasn't found on PATH.")


def run(
    name: str, *, root: Path | None, org: str | None, github_org: str | None = None, yes: bool, pat: str | None = None
) -> None:
    """`bdt find-repo <name>`: search `root` (default `work_dir()`) for a local
    match, then -- if there isn't one -- `org`'s Azure DevOps repos and/or
    `github_org`'s GitHub repos, offering to clone a single remote-only match
    into `root/<project>/<repo>` (ADO) or `root/github/<repo>` (GitHub).
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

    if not org and not github_org:
        sys.exit(
            f"No local repo matching '{name}' found under {search_root}. "
            "Set AZDO_ORG (or BMS_ORG) to also search Azure DevOps, GITHUB_ORG (or BMS_GITHUB_ORG) to "
            "also search GitHub, or pass --org/--github-org."
        )

    # Both are independent, network-I/O-bound org searches -- run them concurrently
    # rather than paying their full latency back-to-back when both are given.
    remote_matches: list[RemoteRepo] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        searches = []
        if org:
            az = require_az()
            searches.append(pool.submit(find_remote, az, org, name))
        if github_org:
            gh = require_gh()
            searches.append(pool.submit(find_github, gh, github_org, name))
        for search in searches:
            remote_matches += search.result()

    # An exact match from one source beats a substring match from the other --
    # find_remote/find_github already apply exact-first within their own source.
    remote_matches = _prefer_exact(remote_matches, name, key=lambda r: r.name)

    if not remote_matches:
        searched = []
        if org:
            searched.append(f"the '{org}' Azure DevOps org")
        if github_org:
            searched.append(f"the '{github_org}' GitHub org")
        sys.exit(f"No repo matching '{name}' found locally under {search_root} or in " + " or ".join(searched) + ".")

    for r in remote_matches:
        print(f"{r.project}/{r.name}  {r.remote_url}")

    if len(remote_matches) > 1:
        sys.exit("\nMultiple matches -- narrow the name or clone manually.")

    match = remote_matches[0]
    dest = search_root / "github" / match.name if match.source == "github" else search_root / match.project / match.name
    if dest.exists():
        sys.exit(f"{dest} already exists -- remove it or clone manually.")

    if not yes:
        # Non-interactive callers (CI, an AI-agent caller, stdin piped/closed)
        # never get an input() prompt -- that would just hang or silently
        # eat piped data. They see the match above and can pass --yes.
        if not sys.stdin.isatty():
            print(f"\nNot found locally. Pass --yes to clone into {dest}.")
            return
        answer = input(f"\nNot found locally. Clone into {dest}? [y/N]: ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Not cloned.")
            return

    if match.source == "github":
        result = clone_github(require_gh(), f"{match.project}/{match.name}", dest)
    else:
        result = clone(match.remote_url, dest, auth_header(pat))
    if result.returncode != 0:
        raise SystemExit(result.returncode)
