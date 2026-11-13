"""`bdt find-repo <name>`: locate a repo by name, locally first and then in
an Azure DevOps org and/or a GitHub org, without needing the full
`ALL_REPOS.md` org sync that the `cross-repo-discovery` skill
(bmsuisse/skills) used to require.

`bdt list-repo` is the browse counterpart: every repo of the same orgs (or the local clones) with its
newest commit and a one-line description.

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

import base64
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar
from urllib.parse import quote, unquote, urlparse

import requests

from .ado_auth import auth_header
from .cli_tools import HTTP_TIMEOUT_SECS, require_gh
from .github_api import GitHubClient, resolve_token
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
class CommitInfo:
    """Newest commit of a repo; every field is "" where the source doesn't tell."""

    id: str = ""
    date: str = ""  # ISO 8601
    author: str = ""


@dataclass(frozen=True)
class RemoteRepo:
    project: str
    name: str
    remote_url: str
    source: str = "ado"
    description: str = ""
    last_commit: CommitInfo = CommitInfo()
    disabled: bool = False


ADO_API_VERSION = "7.1"
ADO_PAGE_SIZE = 100
ADO_DETAIL_WORKERS = 16


def _ado_get(url: str, headers: dict[str, str], params: dict[str, str | int] | None = None) -> tuple[bool, dict | str]:
    """Non-fatal Azure DevOps REST GET: (True, parsed JSON) or (False, error message).

    Non-fatal on purpose: `find_remote`'s per-project fan-out must survive one project
    failing (e.g. no access) instead of aborting the whole org search.
    """
    try:
        r = requests.get(
            url, headers=headers, params={"api-version": ADO_API_VERSION, **(params or {})}, timeout=HTTP_TIMEOUT_SECS
        )
    except requests.RequestException as e:
        return False, str(e)
    if r.status_code >= 400:
        try:
            msg = r.json().get("message") or r.reason
        except (ValueError, AttributeError):
            msg = r.reason
        if r.status_code in (401, 203):
            msg = f"{msg} (not authorized -- pass --pat / set AZURE_DEVOPS_EXT_PAT, or `az login`)"
        return False, f"HTTP {r.status_code}: {msg}"
    try:
        return True, r.json()
    except ValueError:
        # Azure DevOps answers 200 with an HTML sign-in page when the credentials are rejected.
        return False, "unexpected non-JSON response (credentials rejected?)"


def _ado_projects(org: str, headers: dict[str, str]) -> list[dict]:
    projects: list[dict] = []
    skip = 0
    while True:
        ok, data = _ado_get(
            f"https://dev.azure.com/{quote(org)}/_apis/projects", headers, {"$top": ADO_PAGE_SIZE, "$skip": skip}
        )
        if not ok:
            sys.exit(f"Listing Azure DevOps projects in '{org}' failed:\n{str(data).strip()}")
        assert isinstance(data, dict)
        page = data.get("value", [])
        projects.extend(page)
        if len(page) < ADO_PAGE_SIZE:
            return projects
        skip += len(page)


def _ado_all_repos(org: str, headers: dict[str, str]) -> list[RemoteRepo]:
    """Every repo of every project in `org`.

    Azure DevOps has no "search repos by name across the whole org" API, so
    (as the old skill's script did) this lists every project and then every
    project's repos -- there's no cheaper way to do it. The per-project calls
    run concurrently (each is just an HTTP request waiting on network I/O)
    since with dozens of projects a serial fan-out is the dominant cost.
    """
    projects = _ado_projects(org, headers)

    def repos_for(project: dict) -> list[RemoteRepo]:
        ok, data = _ado_get(
            f"https://dev.azure.com/{quote(org)}/{quote(project['name'])}/_apis/git/repositories", headers
        )
        if not ok:
            print(f"warning: couldn't list repos for project '{project['name']}': {str(data).strip()}", file=sys.stderr)
            return []
        assert isinstance(data, dict)
        return [
            RemoteRepo(project["name"], r["name"], r["remoteUrl"], disabled=bool(r.get("isDisabled")))
            for r in data.get("value", [])
        ]

    all_repos: list[RemoteRepo] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for repos in pool.map(repos_for, projects):
            all_repos.extend(repos)
    return all_repos


def find_remote(org: str, name: str, pat: str | None = None) -> list[RemoteRepo]:
    """Search every project in `org` for a repo matching `name` -- see `_prefer_exact`."""
    headers = auth_header(pat)  # resolved once, shared by the worker threads
    return _prefer_exact(_ado_all_repos(org, headers), name, key=lambda r: r.name)


def _github_all_repos(client: GitHubClient, org: str) -> list[RemoteRepo]:
    """Every repo of `org`'s GitHub account.

    Unlike Azure DevOps, GitHub has a single flat list of repos per org/user
    (no per-project split), so one paginated listing covers the whole org. `org` may
    also be a user account: `orgs/{org}/repos` 404s for those, so fall back to `users/{org}/repos`.
    """
    org_path = quote(org, safe="")
    path, params = f"orgs/{org_path}/repos", None
    if client.request("GET", path, ok=(404,), params={"per_page": 1}).status_code == 404:
        # `users/{login}/repos` lists only public repos, even for the token's own account
        if client.get("user").get("login", "").lower() == org.lower():
            path, params = "user/repos", {"affiliation": "owner"}
        else:
            path = f"users/{org_path}/repos"
    items = client.paginate(path, params, limit=1000)
    return [RemoteRepo(org, r["name"], r["html_url"], source="github") for r in items]


def find_github(client: GitHubClient, org: str, name: str) -> list[RemoteRepo]:
    """Search `org`'s GitHub repos for a match -- see `_prefer_exact`."""
    return _prefer_exact(_github_all_repos(client, org), name, key=lambda r: r.name)


DESCRIPTION_MAX_LEN = 120
README_NAMES = ("README.md", "readme.md", "README.markdown", "README")
# Lines that open a README but say nothing about the repo: badges, images, HTML, tables, fences, rules.
_README_NOISE_PREFIXES = ("![", "[![", "<", "|", "`", "---", "===", "***")
# Numbered-list lines: the default Azure DevOps README template ("1. Installation process 2. ...") is all of them.
_NUMBERED_LINE = re.compile(r"\d+[.)]\s")
_SENTENCE_END = re.compile(r"(?<!\d)[.!?](\s|$)")


def _is_readme_noise(line: str) -> bool:
    return (
        line.startswith(_README_NOISE_PREFIXES)
        or line.upper().startswith("TODO")
        or bool(_NUMBERED_LINE.match(line))
        or not any(c.isalpha() for c in line)
    )


def readme_summary(text: str) -> str:
    """One-line description from a README: the first sentence of its first prose paragraph, else its
    first heading's text.

    Azure DevOps repos have no description field, so the README is the closest thing to one.
    """
    heading = ""
    paragraph: list[str] = []
    for raw in [*text.splitlines(), ""]:  # trailing "" closes a paragraph that runs to the end
        line = raw.strip()
        if line.startswith("#"):
            heading = heading or line.lstrip("#").strip()
            line = ""
        elif _is_readme_noise(line):
            line = ""
        if line:
            paragraph.append(line.lstrip(">-* ").strip())
        elif paragraph:
            break
    summary = " ".join(paragraph)
    match = _SENTENCE_END.search(summary)
    summary = summary[: match.start() + 1] if match else summary or heading
    return summary if len(summary) <= DESCRIPTION_MAX_LEN else summary[: DESCRIPTION_MAX_LEN - 1].rstrip() + "…"


def _ado_details(org: str, repo: RemoteRepo, headers: dict[str, str]) -> RemoteRepo:
    """`repo` plus its default-branch newest commit and a README-derived description (Azure DevOps has neither on the repo)."""
    base = f"https://dev.azure.com/{quote(org)}/{quote(repo.project)}/_apis/git/repositories/{quote(repo.name)}"
    # Failures below are a blank field, not a failed listing (no access, empty repo, no README).
    ok, commits = _ado_get(f"{base}/commits", headers, {"searchCriteria.$top": 1})
    if not (ok and isinstance(commits, dict) and commits.get("value")):
        return repo  # no commits means no README either: don't spend requests on it
    c = commits["value"][0]
    last_commit = CommitInfo(c.get("commitId", ""), c.get("committer", {}).get("date", ""), c.get("author", {}).get("name", ""))
    # JSON is requested explicitly: without it the items API answers with the raw file instead of `content`.
    description = ""
    for name in README_NAMES:
        ok, readme = _ado_get(
            f"{base}/items", {**headers, "Accept": "application/json"}, {"path": f"/{name}", "includeContent": "true"}
        )
        if ok and isinstance(readme, dict):
            description = readme_summary(readme.get("content") or "")
            break
    return replace(repo, description=description, last_commit=last_commit)


_GITHUB_REPOS_QUERY = """
query($login: String!, $after: String) {
  repositoryOwner(login: $login) {
    repositories(first: 100, after: $after, ownerAffiliations: OWNER) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name url description pushedAt isDisabled
        defaultBranchRef { target { ... on Commit { oid committedDate author { name } } } }
      }
    }
  }
}
"""


def _github_repos_with_commits(client: GitHubClient, org: str) -> list[RemoteRepo]:
    """Every repo of `org` (an org or a user) with description and default-branch newest commit, 100 repos per request.

    One GraphQL query per page gets what would take a REST request per repo for the commit. An empty
    repo has no default branch: it keeps its push time.
    """
    repos: list[RemoteRepo] = []
    after: str | None = None
    while True:
        owner = client.graphql(_GITHUB_REPOS_QUERY, login=org, after=after)["repositoryOwner"]
        if owner is None:
            sys.exit(f"GitHub account '{org}' not found (or not visible to your token).")
        page = owner["repositories"]
        for r in page["nodes"]:
            commit = ((r.get("defaultBranchRef") or {}).get("target")) or {}
            last_commit = (
                CommitInfo(commit.get("oid", ""), commit.get("committedDate", ""), (commit.get("author") or {}).get("name") or "")
                if commit
                else CommitInfo(date=r.get("pushedAt") or "")
            )
            repos.append(
                RemoteRepo(
                    org, r["name"], r["url"], source="github", description=r.get("description") or "", last_commit=last_commit, disabled=bool(r.get("isDisabled"))
                )
            )
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        after = page["pageInfo"]["endCursor"]


def list_remote(
    *, org: str | None, github_org: str | None, pat: str | None = None, fast: bool = False, include_disabled: bool = False
) -> list[RemoteRepo]:
    """Every repo of `org` (Azure DevOps) and/or `github_org` (GitHub), sorted by project and name.

    Disabled repos are left out unless `include_disabled` (they can't be cloned, and their commits and
    README aren't readable, so they get no description or commit either). GitHub comes with description and
    newest commit in one query per 100 repos. Azure DevOps costs a commit request plus up to a few README
    requests per repo, run concurrently like the project fan-out, unless `fast`.
    """
    repos: list[RemoteRepo] = []
    headers = auth_header(pat) if org else {}
    gh = require_gh() if github_org else None
    with ThreadPoolExecutor(max_workers=2) as pool:
        searches = []
        if org:
            searches.append(pool.submit(_ado_all_repos, org, headers))
        if github_org and gh:
            searches.append(pool.submit(_github_repos_with_commits, gh, github_org))
        for search in searches:
            repos += search.result()

    if not include_disabled:
        repos = [r for r in repos if not r.disabled]

    if org and not fast:

        def details(r: RemoteRepo) -> RemoteRepo:
            return r if r.source != "ado" or r.disabled else _ado_details(org, r, headers)

        with ThreadPoolExecutor(max_workers=ADO_DETAIL_WORKERS) as pool:
            repos = list(pool.map(details, repos))
    return sorted(repos, key=lambda r: (r.project.lower(), r.name.lower()))


def _local_last_commit(path: Path) -> CommitInfo:
    """Newest commit across all of a clone's branches (empty for a clone without commits)."""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--all", "--format=%H%x00%cI%x00%an"], cwd=path, capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        sys.exit("'git' is required for this command but wasn't found on PATH.")
    parts = r.stdout.strip().split("\x00")
    return CommitInfo(*parts) if len(parts) == 3 else CommitInfo()


@dataclass(frozen=True)
class LocalRepo:
    path: Path
    description: str
    last_commit: CommitInfo
    origin_url: str  # `remote.origin.url`, "" if the clone has no origin


def _local_origin_url(path: Path) -> str:
    try:
        r = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=path, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        sys.exit("'git' is required for this command but wasn't found on PATH.")
    return r.stdout.strip()


def list_local(root: Path) -> list[LocalRepo]:
    """Every clone under `root` with its README one-liner, newest commit and origin URL."""
    result = []
    for path in sorted(find_repos(root)):
        readme = next((path / n for n in README_NAMES if (path / n).is_file()), None)
        text = readme.read_text(encoding="utf-8", errors="replace") if readme else ""
        result.append(LocalRepo(path, readme_summary(text), _local_last_commit(path), _local_origin_url(path)))
    return result


_SCP_LIKE_REMOTE = re.compile(r"^[\w.-]+@([\w.-]+):(.+)$")


def web_url(git_url: str) -> str:
    """Browser URL of the Azure DevOps / GitHub repo a git remote (https, `ssh://` or scp-like) points to; "" if unrecognised."""
    scp = _SCP_LIKE_REMOTE.match(git_url)
    if scp:
        host, path = scp.groups()
    else:
        parsed = urlparse(git_url)
        host, path = parsed.hostname or "", parsed.path
    host = host.lower()
    parts = [unquote(p) for p in path.strip("/").split("/")]
    # Legacy Azure DevOps hosts: https://org.visualstudio.com/[DefaultCollection/]project/_git/repo, ssh vs-ssh.visualstudio.com
    if host == "vs-ssh.visualstudio.com":
        host = "ssh.dev.azure.com"
    elif host.endswith(".visualstudio.com"):
        org, host = host.removesuffix(".visualstudio.com"), "dev.azure.com"
        parts = [org, *(parts[1:] if parts[0].lower() == "defaultcollection" else parts)]
    if host == "github.com" and len(parts) == 2:
        return f"https://github.com/{quote(parts[0], safe='')}/{quote(parts[1].removesuffix('.git'), safe='')}"
    if host in ("dev.azure.com", "ssh.dev.azure.com"):
        if len(parts) == 4 and parts[2] == "_git":  # https://org@dev.azure.com/org/project/_git/repo
            org, project, repo = parts[0], parts[1], parts[3]
        elif len(parts) == 3 and parts[1] == "_git":  # a repo named like its project: org/_git/repo
            org, project, repo = parts[0], parts[2], parts[2]
        elif len(parts) == 4 and parts[0] == "v3":  # ssh: v3/org/project/repo
            org, project, repo = parts[1], parts[2], parts[3]
        else:
            return ""
        return "https://dev.azure.com/" + "/".join(quote(x, safe="") for x in (org, project, "_git", repo))
    return ""


def local_clones_by_web_url(root: Path) -> dict[str, Path]:
    """Clones under `root` keyed by their origin's lower-cased `web_url` (the first clone wins if a repo is cloned twice)."""
    clones: dict[str, Path] = {}
    for path in sorted(find_repos(root)):
        key = web_url(_local_origin_url(path)).lower()
        if key:
            clones.setdefault(key, path)
    return clones


def _remote_urls(repo: RemoteRepo) -> tuple[str, str]:
    """(git clone URL, browser URL) of `repo`."""
    if repo.source == "github":  # `remote_url` is the html_url
        return repo.remote_url.removesuffix(".git") + ".git", repo.remote_url
    return repo.remote_url, web_url(repo.remote_url)


def run_list(
    *,
    root: Path | None,
    org: str | None,
    github_org: str | None = None,
    local: bool = False,
    no_local: bool = False,
    include_disabled: bool = False,
    fast: bool = False,
    as_json: bool = False,
    pat: str | None = None,
) -> None:
    """`bdt list-repo`: print every repo with its last commit date and description.

    Lists `org`'s Azure DevOps repos and/or `github_org`'s GitHub repos; with neither (or with
    `local`) lists the clones under `root` (default `work_dir()`) instead. The JSON of a remote
    listing also names each repo's local clone (`location`, null if not cloned) unless `no_local`.
    """
    if local and no_local:
        sys.exit("--local and --no-local contradict each other.")
    entries: list[dict[str, str | bool | None]]
    search_root = root or work_dir()
    if local or not (org or github_org):
        if no_local:
            sys.exit("--no-local needs an org to list (AZDO_ORG / GITHUB_ORG or --org / --github-org).")
        if not search_root.is_dir():
            sys.exit(
                f"Local work dir {search_root} doesn't exist. Set AZDO_WORK_DIR (or BMS_WORK_DIR) to "
                "where your repos are cloned, or pass --root."
            )
        if not local:
            print("No org configured (AZDO_ORG / GITHUB_ORG or --org / --github-org): listing local clones.", file=sys.stderr)
        entries = [
            {
                "name": r.path.name,
                "git_url": r.origin_url,
                "web_url": web_url(r.origin_url),
                "location": str(r.path),
                **_commit_fields(r.last_commit),
                "description": r.description,
            }
            for r in list_local(search_root)
        ]
        labels = [str(e["location"]) for e in entries]
        descriptions = [str(e["description"]) for e in entries]
    else:
        remote = list_remote(org=org, github_org=github_org, pat=pat, fast=fast, include_disabled=include_disabled)
        # Without a work dir there is simply nothing cloned; only the --local listing insists that it exists.
        clones = {} if no_local or not search_root.is_dir() else local_clones_by_web_url(search_root)
        entries = []
        for r in remote:
            git_url, browse_url = _remote_urls(r)
            entry: dict[str, str | bool | None] = {
                "project": r.project,
                "name": r.name,
                "source": r.source,
                "disabled": r.disabled,
                "git_url": git_url,
                "web_url": browse_url,
            }
            if not no_local:
                clone_path = clones.get(browse_url.lower())
                entry["location"] = str(clone_path) if clone_path else None
            entries.append({**entry, **_commit_fields(r.last_commit), "description": r.description})
        labels = [f"{r.project}/{r.name}" for r in remote]
        descriptions = ["(DISABLED) " * r.disabled + r.description for r in remote]

    if as_json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return
    # Descriptions and authors are remote-controlled text: don't let them drive the terminal (JSON above keeps them verbatim).
    labels = [_printable(label) for label in labels]
    authors = [_printable(str(e["last_commit_author"])) for e in entries]
    label_w, author_w = max(map(len, labels), default=0), max(map(len, authors), default=0)
    for label, author, description, e in zip(labels, authors, descriptions, entries):
        commit = f"{str(e['last_commit_date'])[:10]:<10}  {str(e['last_commit_id'])[:8]:<8}  {author:<{author_w}}"
        print(f"{label:<{label_w}}  {commit}  {_printable(description)}".rstrip())


def _printable(text: str) -> str:
    """`text` with control characters (ESC, tabs, newlines, ...) replaced by spaces."""
    return "".join(c if c.isprintable() else " " for c in text)


def _commit_fields(c: CommitInfo) -> dict[str, str | None]:
    return {"last_commit_id": c.id, "last_commit_date": c.date, "last_commit_author": c.author}


def clone(remote_url: str, dest: Path, auth: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Clone `remote_url` (an HTTPS Azure DevOps or GitHub URL) into `dest`.

    `auth` (from `ado_auth.auth_header`) is passed to git as an HTTP
    Authorization header via `GIT_CONFIG_*` env vars rather than a
    `-c http.extraheader=...` CLI argument -- the latter would leak the PAT
    to anyone able to see this process's argv (`ps aux`, /proc/<pid>/cmdline).
    """
    env = os.environ.copy()
    if auth:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)  # append after any config the caller already injects
        env["GIT_CONFIG_COUNT"] = str(n + 1)
        env[f"GIT_CONFIG_KEY_{n}"] = "http.extraheader"
        env[f"GIT_CONFIG_VALUE_{n}"] = f"AUTHORIZATION: {auth['Authorization']}"
    try:
        return subprocess.run(["git", "clone", remote_url, str(dest)], env=env, check=False)
    except FileNotFoundError:
        sys.exit("'git' is required for this command but wasn't found on PATH.")


def clone_github(full_name: str, dest: Path, token: str | None = None) -> subprocess.CompletedProcess:
    """`git clone` a GitHub repo (`owner/name`) over HTTPS, authenticated with the GitHub token.

    The token goes in as an `http.extraheader` (Basic `x-access-token:<token>`, via `clone()`'s
    env-var plumbing) -- never in the URL or argv. Deliberately not given `ado_auth.auth_header`:
    an Azure DevOps credential must never be sent to github.com.
    """
    basic = base64.b64encode(f"x-access-token:{token or resolve_token()}".encode()).decode()
    return clone(f"https://github.com/{full_name}.git", dest, {"Authorization": f"Basic {basic}"})


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
            searches.append(pool.submit(find_remote, org, name, pat))
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
        result = clone_github(f"{match.project}/{match.name}", dest)
    else:
        result = clone(match.remote_url, dest, auth_header(pat))
    if result.returncode != 0:
        raise SystemExit(result.returncode)
