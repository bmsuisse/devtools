"""Git-remote inspection shared by the Azure DevOps commands."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from urllib.parse import unquote


class NotAzureDevOpsRemoteError(Exception):
    pass


@dataclass(frozen=True)
class AdoRemote:
    org: str
    project: str
    repo: str


def current_branch(cwd: str | None = None) -> str:
    return subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True, cwd=cwd).strip()


def origin_url(cwd: str | None = None) -> str:
    return subprocess.check_output(["git", "remote", "get-url", "origin"], text=True, cwd=cwd).strip()


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


def current_ado_remote(cwd: str | None = None) -> AdoRemote:
    return parse_ado_remote(origin_url(cwd))
