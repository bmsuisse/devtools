"""Git-remote inspection: figure out which host (Azure DevOps or GitHub) the
current repo's `origin` points at, and parse out its org/project/repo (ADO)
or owner/repo (GitHub) so callers don't have to.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import unquote


class NotAzureDevOpsRemoteError(Exception):
    pass


class NotGitHubRemoteError(Exception):
    pass


class UnknownRemoteError(Exception):
    pass


@dataclass(frozen=True)
class AdoRemote:
    org: str
    project: str
    repo: str


@dataclass(frozen=True)
class GitHubRemote:
    owner: str
    repo: str


def _run_git(args: list[str], cwd: str | None = None) -> str:
    try:
        return subprocess.check_output(["git", *args], encoding="utf-8", cwd=cwd).strip()
    except FileNotFoundError:
        sys.exit("'git' is required for this command but wasn't found on PATH.")
    except subprocess.CalledProcessError as e:
        sys.exit((e.stderr or e.stdout or str(e)).strip() if isinstance(e.stderr, str) else str(e))


def current_branch(cwd: str | None = None) -> str:
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)


def origin_url(cwd: str | None = None) -> str:
    return _run_git(["remote", "get-url", "origin"], cwd=cwd)


def parse_ado_remote(url: str) -> AdoRemote:
    """Parse an Azure DevOps org/project/repo out of a git remote URL.

    Handles SSH (git@ssh.dev.azure.com:v3/org/project/repo), dev.azure.com
    HTTPS, and the older *.visualstudio.com HTTPS form. Project/repo names
    are unquoted since ADO allows spaces and punctuation (e.g. "BMS - Data").
    """
    if url.startswith("git@ssh.dev.azure.com"):
        path = url.split(":", 1)[1]
        parts = path.strip("/").split("/")
        if parts[0] == "v3":
            parts = parts[1:]
        return AdoRemote(parts[0], unquote(parts[1]), unquote(parts[2]))

    parts = url.rstrip("/").split("/")
    if "dev.azure.com" in url:
        for i, part in enumerate(parts):
            if "dev.azure.com" in part:
                org = parts[i + 1]
                project = unquote(parts[i + 2])
                if i + 4 < len(parts) and parts[i + 3] == "_git":
                    repo = unquote(parts[i + 4])
                else:
                    repo = unquote(parts[i + 3])
                return AdoRemote(org, project, repo)
    elif "visualstudio.com" in url:
        org = parts[2].split(".")[0]
        project = unquote(parts[3])
        if len(parts) > 5 and parts[4] == "_git":
            repo = unquote(parts[5])
        else:
            repo = unquote(parts[4])
        return AdoRemote(org, project, repo)

    raise NotAzureDevOpsRemoteError(f"Could not parse Azure DevOps info from remote URL: {url}")


# Matches git@github.com:owner/repo(.git), ssh://git@github.com/owner/repo(.git),
# and https://[user@]github.com/owner/repo(.git).
_GITHUB_RE = re.compile(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$")


def parse_github_remote(url: str) -> GitHubRemote:
    match = _GITHUB_RE.search(url)
    if not match:
        raise NotGitHubRemoteError(f"Could not parse GitHub owner/repo from remote URL: {url}")
    return GitHubRemote(match.group(1), match.group(2))


def parse_remote(url: str) -> AdoRemote | GitHubRemote:
    """Parse whichever of GitHub or Azure DevOps `url` points at."""
    if "github.com" in url:
        return parse_github_remote(url)
    try:
        return parse_ado_remote(url)
    except NotAzureDevOpsRemoteError:
        raise UnknownRemoteError(
            f"'{url}' doesn't look like a GitHub or Azure DevOps remote — only those two are supported."
        ) from None


def current_remote(cwd: str | None = None) -> AdoRemote | GitHubRemote:
    return parse_remote(origin_url(cwd))


def current_ado_remote(cwd: str | None = None) -> AdoRemote:
    return parse_ado_remote(origin_url(cwd))
