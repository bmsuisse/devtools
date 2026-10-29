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
from dataclasses import dataclass
from pathlib import Path

import requests

from .gitrepo import AdoRemote

DEFAULT_COMMIT_DEPTH = 1000
STAKEHOLDER = "stakeholder"
_ENTITLEMENT_API_VERSION = "7.1-preview.3"
# Statuses that mean "this token may not (or cannot) ask", not "the user doesn't exist"
_UNAVAILABLE_STATUSES = (401, 403, 404)


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
    # %aE/%aN/%cE/%cN honour .mailmap
    log = subprocess.run(
        ["git", "log", "-n", str(depth), "--format=%aE%x09%aN%x09%cE%x09%cN"], capture_output=True, text=True, cwd=cwd, check=False
    )
    if log.returncode != 0:
        return None
    rows = [line.split("\t") for line in log.stdout.splitlines() if line]
    shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], capture_output=True, text=True, cwd=cwd, check=False)
    if shallow.stdout.strip() == "true" and len(rows) < depth:
        return None
    return CommitAuthors(
        emails=frozenset(v.casefold() for row in rows for v in (row[0], row[2]) if v),
        names=frozenset(v.casefold() for row in rows for v in (row[1], row[3]) if v),
        depth=depth,
    )


def get_license(session: requests.Session, remote: AdoRemote, identity_id: str) -> str | None:
    """The account license type of an identity, lowercased (`stakeholder`, `express`, `advanced`, `none`, ...), or None
    when the entitlement API doesn't answer (missing PAT scope, user not in the org, ...)."""
    r = session.get(
        f"https://vsaex.dev.azure.com/{remote.org}/_apis/userentitlements/{identity_id}",
        params={"api-version": _ENTITLEMENT_API_VERSION},
    )
    if r.status_code in _UNAVAILABLE_STATUSES:
        return None
    r.raise_for_status()
    license_type = (r.json().get("accessLevel") or {}).get("accountLicenseType")
    return license_type.casefold() if license_type else None


def classify(license_type: str | None, authors: CommitAuthors | None, email: str | None, name: str | None) -> Verdict:
    """Stakeholder license -> business user; else, no commit by this person in `authors` -> business user."""
    if license_type == STAKEHOLDER:
        return Verdict(True, "Stakeholder license")
    known_license = f"{license_type} license" if license_type else "license unknown"
    if authors is None:
        return Verdict(None, f"{known_license}, commit history unavailable")
    if authors.contains(email, name):
        return Verdict(False, f"{known_license}, has commits")
    return Verdict(True, f"{known_license}, no commits in the last {authors.depth} of this repo")


class BusinessUserDetector:
    """Judges work item creators (the `System.CreatedBy` identity dict), looking up each license once and reading
    the git history once per detector."""

    def __init__(
        self, session: requests.Session, remote: AdoRemote, commit_depth: int = DEFAULT_COMMIT_DEPTH, repo_dir: Path | None = None
    ) -> None:
        self._session = session
        self._remote = remote
        self._commit_depth = commit_depth
        self._repo_dir = repo_dir
        self._authors: CommitAuthors | None = None
        self._authors_read = False
        self._licenses: dict[str, str | None] = {}

    def _commit_authors(self) -> CommitAuthors | None:
        if not self._authors_read:
            self._authors = read_commit_authors(self._commit_depth, self._repo_dir)
            self._authors_read = True
        return self._authors

    def _license(self, identity_id: str | None) -> str | None:
        if not identity_id:
            return None
        if identity_id not in self._licenses:
            self._licenses[identity_id] = get_license(self._session, self._remote, identity_id)
        return self._licenses[identity_id]

    def verdict(self, creator: dict | str | None) -> Verdict:
        """`creator` is the `System.CreatedBy` field: an identity dict (`id`, `uniqueName`, `displayName`)."""
        if not isinstance(creator, dict):
            return Verdict(None, "creator unknown")
        license_type = self._license(creator.get("id"))
        if license_type == STAKEHOLDER:
            return classify(license_type, None, None, None)
        return classify(license_type, self._commit_authors(), creator.get("uniqueName"), creator.get("displayName"))
