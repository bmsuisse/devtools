"""Raw-HTTP GitHub client (REST + GraphQL), replacing the `gh` CLI.

Auth: `GH_TOKEN` / `GITHUB_TOKEN`, else (last resort) whatever `gh auth token` prints, so an
existing `gh auth login` keeps working without exporting a token. Failures `sys.exit` with the
API's own message, like the subprocess calls this replaces, so best-effort callers that catch
`SystemExit` keep working.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any
from urllib.parse import urlparse

import requests

from .cli_tools import CLI_TIMEOUT_SECS, HTTP_TIMEOUT_SECS

API_URL = "https://api.github.com"
GH_TOKEN_HINT = "Set GH_TOKEN (or GITHUB_TOKEN) to a GitHub token, or run `gh auth login`."


def _token_from_gh_cli() -> str | None:
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        r = subprocess.run([gh, "auth", "token"], capture_output=True, encoding="utf-8", timeout=CLI_TIMEOUT_SECS)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def resolve_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or _token_from_gh_cli()
    if not token:
        sys.exit(f"No GitHub token found.\n{GH_TOKEN_HINT}")
    return token


class GitHubClient:
    """One authenticated `requests.Session` against api.github.com.

    `owner`/`repo` default to the current directory's `origin` remote, resolved lazily (only
    calls that address "the current repo" need it, so e.g. an org-wide search works outside a checkout).
    """

    def __init__(self, token: str, owner: str | None = None, repo: str | None = None, base_url: str = API_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._owner = owner
        self._repo = repo
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "bdt",
            }
        )

    @property
    def owner(self) -> str:
        self._resolve_repo()
        assert self._owner
        return self._owner

    @property
    def repo(self) -> str:
        self._resolve_repo()
        assert self._repo
        return self._repo

    @property
    def repo_path(self) -> str:
        return f"repos/{self.owner}/{self.repo}"

    def _resolve_repo(self) -> None:
        if self._owner and self._repo:
            return
        from .gitrepo import parse_github_remote, origin_url

        remote = parse_github_remote(origin_url())
        self._owner, self._repo = remote.owner, remote.repo

    def request(self, method: str, path: str, *, ok: tuple[int, ...] = (), **kwargs: Any) -> requests.Response:
        """`method path` (relative to the API root, or a full URL); exits with GitHub's error message on a non-2xx
        status (except those listed in `ok`, which are returned for the caller to inspect)."""
        if path.startswith("http"):
            url = path
            target, base = urlparse(url), urlparse(self.base_url)
            if target.scheme != "https" or target.netloc != base.netloc:
                # the session's Authorization header must never reach another host
                sys.exit(f"Refusing to send the GitHub token to {target.netloc or url!r}: not {base.netloc} over https")
        else:
            url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            r = self.session.request(method, url, timeout=HTTP_TIMEOUT_SECS, **kwargs)
        except requests.RequestException as e:
            sys.exit(f"GitHub {method} {path} failed: {e}")
        if r.status_code >= 400 and r.status_code not in ok:
            sys.exit(f"GitHub {method} {path} failed (HTTP {r.status_code}): {_error_message(r)}")
        return r

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).json()

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        r = self.request("POST", path, json=body, **kwargs)
        return r.json() if r.content else None

    def patch(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        r = self.request("PATCH", path, json=body, **kwargs)
        return r.json() if r.content else None

    def put(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        r = self.request("PUT", path, json=body, **kwargs)
        return r.json() if r.content else None

    def delete(self, path: str, **kwargs: Any) -> None:
        self.request("DELETE", path, **kwargs)

    def paginate(self, path: str, params: dict[str, Any] | None = None, *, items_key: str | None = None, limit: int | None = None) -> list[Any]:
        """Every item of a list endpoint, following `Link: rel="next"`. `items_key` for the
        wrapped shape (e.g. search's `{"items": [...]}`); stops early once `limit` items are in."""
        out: list[Any] = []
        url: str | None = path
        query: dict[str, Any] | None = {"per_page": 100, **(params or {})}
        while url:
            r = self.request("GET", url, params=query)
            data = r.json()
            out.extend(data[items_key] if items_key else data)
            if limit is not None and len(out) >= limit:
                return out[:limit]
            url = r.links.get("next", {}).get("url")
            query = None  # the next-page URL already carries the query string
        return out

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        data = self.post("graphql", {"query": query, "variables": variables})
        if data.get("errors"):
            sys.exit(f"GitHub GraphQL failed: {'; '.join(e.get('message', '?') for e in data['errors'])}")
        return data["data"]


def _error_message(r: requests.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text.strip() or r.reason or "request failed"
    msg = body.get("message", r.reason) if isinstance(body, dict) else str(body)
    details = "; ".join(str(e.get("message") or e) for e in body.get("errors", []) if e) if isinstance(body, dict) else ""
    return f"{msg} ({details})" if details else msg


def get_client() -> GitHubClient:
    return GitHubClient(resolve_token())
