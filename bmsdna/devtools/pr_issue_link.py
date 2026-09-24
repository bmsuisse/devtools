"""Parse `bdt pr create --issue` references and scan a PR body for issue/work-item mentions.

Both feed the same downstream treatment (see `cli.py::pr_create`): link the issue/work item to
the PR and label it `pr-available` (a GitHub label, or an Azure DevOps tag -- Azure DevOps has no
label concept of its own, tags are the closest equivalent, same as `ado_issue.py`'s `--tag`).

An issue number alone is only unique within a single tracker -- same reasoning `gh_issue.py`'s
`_extract_issue_numbers` uses for board membership -- so a URL is only accepted here when it
names *this* repo's own tracker (same GitHub owner/repo, or same Azure DevOps org). A URL for
some other repo/org is rejected/ignored rather than silently acted on.
"""

from __future__ import annotations

import re

from .gitrepo import AdoRemote, GitHubRemote

PR_AVAILABLE_LABEL = "pr-available"

# GitHub's own PR-body/commit-message closing keywords -- see
# https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
_GITHUB_KEYWORD_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)", re.IGNORECASE)

# Matches a GitHub issue URL, e.g. https://github.com/owner/repo/issues/42. The `(?<![\w-])`
# lookbehind anchors "github.com" to an actual host boundary (start of string, `//`, or a `.`
# subdomain separator) -- without it, a URL on an unrelated domain that merely *contains* the
# substring "github.com" (e.g. https://notgithub.com/owner/repo/issues/42, or any host ending in
# "-github.com") would be misidentified as pointing at github.com itself.
_GITHUB_ISSUE_URL_RE = re.compile(r"(?<![\w-])github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)")

# Matches an Azure DevOps work item URL, e.g. https://dev.azure.com/org/project/_workitems/edit/42
# (also the older org.visualstudio.com host form). Same host-boundary reasoning as
# `_GITHUB_ISSUE_URL_RE` for the "dev.azure.com" branch; the "*.visualstudio.com" branch's
# `[^./\s]+` group already only captures a single dot-delimited label, so it can't be fooled the
# same way.
_ADO_WORK_ITEM_URL_RE = re.compile(r"(?:(?<![\w-])dev\.azure\.com/([^/\s]+)|([^./\s]+)\.visualstudio\.com)/\S*_workitems/edit/(\d+)")


def parse_issue_ref(ref: str, remote: AdoRemote | GitHubRemote) -> int:
    """A bare issue/work-item number, or a URL naming *this* repo's own tracker, -> its number.

    Raises ValueError (with a message suitable to surface via `typer.BadParameter`) for anything
    else: an unparseable value, or a URL for a different repo/org than `remote`.
    """
    ref = ref.strip()
    if ref.isdigit():
        return int(ref)

    if isinstance(remote, GitHubRemote):
        match = _GITHUB_ISSUE_URL_RE.search(ref)
        if match and match.group(1).casefold() == remote.owner.casefold() and match.group(2).casefold() == remote.repo.casefold():
            return int(match.group(3))
        raise ValueError(f"'{ref}' isn't a bare issue number or a github.com/{remote.owner}/{remote.repo}/issues/<N> URL")

    match = _ADO_WORK_ITEM_URL_RE.search(ref)
    if match:
        org = match.group(1) or match.group(2)
        if org and org.casefold() == remote.org.casefold():
            return int(match.group(3))
    raise ValueError(f"'{ref}' isn't a bare work item number or a dev.azure.com/{remote.org}/.../_workitems/edit/<N> URL")


def github_body_already_closes(body: str, issue_number: int) -> bool:
    """True if `body` already contains a GitHub closing-keyword reference (`Fixes`/`Closes`/
    `Resolves #N`, any tense/plural form) to `issue_number`.

    Deliberately narrower than "the body mentions #N anywhere" -- a plain, non-keyword mention
    (e.g. "see #42 for background") doesn't make GitHub treat the PR as genuinely linked to that
    issue (no Development-sidebar entry, no auto-close on merge), so it must not be mistaken for
    one already being present.
    """
    return any(int(m) == issue_number for m in _GITHUB_KEYWORD_RE.findall(body))


def find_issue_refs_in_body(body: str | None, remote: AdoRemote | GitHubRemote) -> list[int]:
    """Issue/work-item numbers mentioned in `body` that belong to *this* repo's own tracker, in
    first-seen order.

    GitHub: `Fixes #N`/`Closes #N`/`Resolves #N` (and their tense/plural variants) plus full
    issue URLs for this owner/repo. Azure DevOps: full work-item URLs for this org only -- Azure
    DevOps has no bare-`#N` keyword syntax of its own for this (unlike GitHub's, `AB#N` is a
    commit-message-only linking convention, not something `pr create`'s body scan is asked to
    understand here).
    """
    if not body:
        return []
    numbers: list[int] = []
    seen: set[int] = set()

    def _add(n: int) -> None:
        if n not in seen:
            seen.add(n)
            numbers.append(n)

    if isinstance(remote, GitHubRemote):
        for match in _GITHUB_KEYWORD_RE.finditer(body):
            _add(int(match.group(1)))
        for match in _GITHUB_ISSUE_URL_RE.finditer(body):
            if match.group(1).casefold() == remote.owner.casefold() and match.group(2).casefold() == remote.repo.casefold():
                _add(int(match.group(3)))
    else:
        for match in _ADO_WORK_ITEM_URL_RE.finditer(body):
            org = match.group(1) or match.group(2)
            if org and org.casefold() == remote.org.casefold():
                _add(int(match.group(3)))

    return numbers
