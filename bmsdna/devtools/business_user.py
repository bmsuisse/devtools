"""Is the creator of an Azure DevOps work item a business user (as opposed to a developer)?

Backs `bdt issue search --business-user` and the `Created by` line of `bdt issue show`. A creator is a
business user when

1. their Azure DevOps license is `Stakeholder` (Member Entitlement API), or
2. they authored none of the last `DEFAULT_COMMIT_DEPTH` commits of the local repo.

The entitlement API needs a PAT with the "Member Entitlement Management (read)" scope; when it is not
reachable the license is treated as unknown and only the commit check decides.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from urllib.parse import quote

import requests

from .gitrepo import AdoRemote

DEFAULT_COMMIT_DEPTH = 1000
STAKEHOLDER = "stakeholder"
_ENTITLEMENT_API_VERSION = "7.1-preview.3"


@dataclass(frozen=True)
class Verdict:
    """`business_user` is None when neither check could tell (no license info, no usable git history)."""

    business_user: bool | None
    reason: str


@dataclass(frozen=True)
class CommitAuthors:
    """Lowercased emails and names of the authors and committers of the last `depth` commits."""

    emails: frozenset[str]
    names: frozenset[str]
    depth: int

    def contains(self, email: str | None, name: str | None) -> bool:
        return bool(email and email.casefold() in self.emails) or bool(name and name.casefold() in self.names)


def read_commit_authors(depth: int = DEFAULT_COMMIT_DEPTH, cwd: Path | None = None) -> CommitAuthors | None:
    """The authors of the last `depth` commits of the repo at `cwd`, or None when that history can't tell who never
    committed: not a git repo, or a shallow clone that has fewer than `depth` commits."""
    # %aE/%aN/%cE/%cN honour .mailmap; \x1f can't occur in a name, and a name can't hold a newline
    log = subprocess.run(
        ["git", "log", "-n", str(depth), "--format=%aE%x1f%aN%x1f%cE%x1f%cN"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        check=False,
    )
    if log.returncode != 0:
        return None
    rows = [fields for line in log.stdout.split("\n") if len(fields := line.split("\x1f")) == 4]
    # `--is-shallow-repository` needs git 2.15; the shallow file's presence works everywhere
    shallow_file = subprocess.run(["git", "rev-parse", "--git-path", "shallow"], capture_output=True, text=True, cwd=cwd, check=False)
    if shallow_file.returncode == 0 and (cwd or Path()).joinpath(shallow_file.stdout.strip()).exists() and len(rows) < depth:
        return None
    return CommitAuthors(
        emails=frozenset(v.casefold() for row in rows for v in (row[0], row[2]) if v),
        names=frozenset(v.casefold() for row in rows for v in (row[1], row[3]) if v),
        depth=depth,
    )


def get_license(session: requests.Session, remote: AdoRemote, identity_id: str) -> str | None:
    """The account license type of an identity, lowercased (`stakeholder`, `express`, `advanced`, `none`, ...), or None
    when the entitlement API doesn't answer (missing PAT scope, user not in the org, outage, ...): the license is
    optional enrichment, and the commit check still has a say without it."""
    try:
        r = session.get(
            f"https://vsaex.dev.azure.com/{remote.org}/_apis/userentitlements/{quote(identity_id, safe='')}",
            params={"api-version": _ENTITLEMENT_API_VERSION},
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        body = r.json()
    except ValueError:  # e.g. an HTML sign-in page for a PAT that may not use this API
        return None
    license_type = ((body.get("accessLevel") if isinstance(body, dict) else None) or {}).get("accountLicenseType")
    return license_type.casefold() if isinstance(license_type, str) else None


def classify(
    license_type: str | None, read_authors: Callable[[], CommitAuthors | None], email: str | None, name: str | None
) -> Verdict:
    """Stakeholder license -> business user; else, no commit by this person in `read_authors()` (only called when the
    license doesn't settle it) -> business user."""
    if license_type == STAKEHOLDER:
        return Verdict(True, "Stakeholder license")
    authors = read_authors()
    known_license = f"{license_type} license" if license_type else "license unknown"
    if authors is None:
        return Verdict(None, f"{known_license}, commit history unavailable")
    if authors.contains(email, name):
        return Verdict(False, f"{known_license}, has commits")
    return Verdict(True, f"{known_license}, no commits in the last {authors.depth} of this repo")


class BusinessUserDetector:
    """Judges work item creators (the `System.CreatedBy` identity dict), looking up each license once and reading
    the git history of the current directory's repo once per detector."""

    def __init__(self, session: requests.Session, remote: AdoRemote, commit_depth: int = DEFAULT_COMMIT_DEPTH) -> None:
        self._session = session
        self._remote = remote
        self._commit_depth = commit_depth
        self._licenses: dict[str, str | None] = {}

    @cached_property
    def _commit_authors(self) -> CommitAuthors | None:
        return read_commit_authors(self._commit_depth)

    def _license(self, identity_id: str | None) -> str | None:
        if not identity_id:
            return None
        if identity_id not in self._licenses:
            self._licenses[identity_id] = get_license(self._session, self._remote, identity_id)
        return self._licenses[identity_id]

    def verdict(self, creator: dict | None) -> Verdict:
        """`creator` is the `System.CreatedBy` field: an identity dict (`id`, `uniqueName`, `displayName`)."""
        if not isinstance(creator, dict):
            return Verdict(None, "creator unknown")
        if "@" not in (creator.get("uniqueName") or ""):
            # build service, service accounts, ...: no license, no commits, and not someone to ask questions
            return Verdict(None, "not a person")
        identity_id = creator.get("id")
        license_type = self._license(identity_id if isinstance(identity_id, str) else None)
        return classify(license_type, lambda: self._commit_authors, creator.get("uniqueName"), creator.get("displayName"))
