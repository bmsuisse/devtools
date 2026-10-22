"""`bdt pr info --pr-id` values: which PR, in which repo.

A bare number is a PR of the current repo. For a PR of another repo (a status integration or an agent working
across repos) an entry carries the repo too: `owner/repo#12` on GitHub, `project/repo#12` or `org/project/repo#12` on
Azure DevOps (the host being the current repo's), or the PR's full web URL, which needs no checkout at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .gitrepo import AdoRemote, GitHubRemote, NotAzureDevOpsRemoteError, parse_ado_remote

FORMS = "a number, owner/repo#N (GitHub), project/repo#N or org/project/repo#N (Azure DevOps), or the PR's URL"


class PrRefError(ValueError):
    pass


@dataclass(frozen=True)
class PrRef:
    pr_id: int
    remote: AdoRemote | GitHubRemote | None  # None: the current repo

    def __str__(self) -> str:
        if isinstance(self.remote, GitHubRemote):
            return f"{self.remote.owner}/{self.remote.repo}#{self.pr_id}"
        if isinstance(self.remote, AdoRemote):
            return f"{self.remote.org}/{self.remote.project}/{self.remote.repo}#{self.pr_id}"
        return f"#{self.pr_id}"


_GITHUB_PR_URL = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:[/?#].*)?$")
_ADO_PR_URL = re.compile(r"^https://(?:dev\.azure\.com/|[^/\s]+\.visualstudio\.com/).*?/_git/[^/\s]+/pullrequest/(\d+)(?:[/?#].*)?$")
_SHORTHAND = re.compile(r"^(.+)#(\d+)$")


def split_values(values: list[str]) -> list[str]:
    """Entries of repeated and/or comma-separated `--pr-id` values, blanks dropped."""
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def parse_pr_ref(text: str, current_remote: Callable[[], AdoRemote | GitHubRemote]) -> PrRef:
    """`current_remote` is only called for the `repo#N` forms, which take their host from it."""
    text = text.strip()
    if re.fullmatch(r"#?\d+", text):
        return PrRef(int(text.lstrip("#")), None)

    if match := _GITHUB_PR_URL.match(text):
        return PrRef(int(match.group(3)), GitHubRemote(match.group(1), match.group(2)))
    if match := _ADO_PR_URL.match(text):
        # the legacy `https://org.visualstudio.com/DefaultCollection/Project/_git/...` form has a collection segment
        url = text.replace("/DefaultCollection/", "/", 1) if "visualstudio.com" in text else text
        try:
            return PrRef(int(match.group(1)), parse_ado_remote(url))
        except (NotAzureDevOpsRemoteError, IndexError):
            raise PrRefError(f"can't read the Azure DevOps repo from '{text}'") from None

    if match := _SHORTHAND.match(text):
        parts = match.group(1).split("/")
        pr_id = int(match.group(2))
        if all(parts) and len(parts) in (2, 3):
            try:
                here = current_remote()
            except (SystemExit, Exception) as e:
                raise PrRefError(
                    f"'{text}' needs to know whether this repo is GitHub or Azure DevOps ({e}); use the PR's URL instead"
                ) from None
            if isinstance(here, GitHubRemote) and len(parts) == 2:
                return PrRef(pr_id, GitHubRemote(parts[0], parts[1]))
            if isinstance(here, AdoRemote):
                if len(parts) == 2:
                    return PrRef(pr_id, AdoRemote(here.org, parts[0], parts[1]))
                return PrRef(pr_id, AdoRemote(parts[0], parts[1], parts[2]))
            raise PrRefError(f"'{text}': GitHub PRs are owner/repo#N")
    raise PrRefError(f"can't read '{text}' as a PR: use {FORMS}")


def canonical(ref: PrRef, here: AdoRemote | GitHubRemote | None) -> PrRef:
    """`ref` as a bare number when it names a PR of the current repo `here` (compared case-insensitively), so that
    `5` and `owner/repo#5` are one PR; `here` None (no checkout) leaves it as it is."""
    if ref.remote is None or here is None or type(ref.remote) is not type(here):
        return ref
    same = [str(getattr(ref.remote, f)).casefold() for f in ref.remote.__dataclass_fields__] == [
        str(getattr(here, f)).casefold() for f in here.__dataclass_fields__
    ]
    return PrRef(ref.pr_id, None) if same else ref
