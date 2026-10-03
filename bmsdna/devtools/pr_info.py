"""`bdt pr info`: a one-shot, machine-readable summary of the current branch's PR.

The bdt Claude Code mod (`mods/bdt-status`) polls this to show a link to the PR and the
issue/work item it closes, plus the build state, above the prompt. Kept as plain data (no
human-oriented formatting) so any other tool -- a shell prompt, a dashboard -- can consume it too.
"""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass, field

import requests

from . import ado_issue, gh_pr, pr_build, pr_issue_link
from .gitrepo import AdoRemote, GitHubRemote

# `build` values: aggregate of every check/pipeline on the PR. "unknown" means the state couldn't
# be determined (Azure DevOps request failed), as opposed to "none": nothing ran at all.
BUILD_PASSING = "passing"
BUILD_FAILING = "failing"
BUILD_PENDING = "pending"
BUILD_WAITING = "waiting"  # paused on a manual approval; nothing resolves it without a human
BUILD_NONE = "none"
BUILD_UNKNOWN = "unknown"


@dataclass(frozen=True)
class IssueRef:
    number: int
    url: str


@dataclass(frozen=True)
class PrInfo:
    number: int
    title: str
    url: str
    state: str  # "open" | "merged" | "closed"
    draft: bool
    build: str
    issues: list[IssueRef] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return asdict(self)


def clean_text(text: str) -> str:
    """`text` without control and format characters (terminal escapes, bidi overrides): a PR title is
    attacker-controlled when the checked-out branch is someone else's PR, and is printed to a terminal."""
    return "".join(c for c in text if unicodedata.category(c)[0] != "C")


def build_state(checks: list[dict]) -> str:
    """Aggregate a GitHub `statusCheckRollup` into one `BUILD_*` value; worst state wins
    (failing > waiting > pending > passing; none if nothing ran). Mirrors `gh_pr.run` (`bdt pr status`): only a "fail"
    bucket is failing, so skipped and cancelled checks (e.g. a run superseded by a newer push)
    don't count against the PR -- the two commands must never disagree about the same PR.
    """
    buckets = {gh_pr.check_bucket(c) for c in checks}
    if buckets <= {"skipping"}:
        # nothing ran (no checks, or every workflow was path-filtered out): not a green build
        return BUILD_NONE
    if "fail" in buckets:
        return BUILD_FAILING
    if "waiting_approval" in buckets:
        return BUILD_WAITING
    if "pending" in buckets:
        return BUILD_PENDING
    return BUILD_PASSING


def github_info(pr: dict, remote: GitHubRemote) -> PrInfo:
    """`pr` is `gh pr view --json` output with `gh_pr.PR_INFO_FIELDS`. Only issues of this repo are listed."""
    # GitHub's own answer to "which issues does merging this close" (closing keywords in the body,
    # or linked in the Development sidebar) -- a mere "related: <issue url>" mention isn't one
    issues = [
        IssueRef(i["number"], i["url"])
        for i in pr.get("closingIssuesReferences") or []
        if i.get("repository", {}).get("name", remote.repo).casefold() == remote.repo.casefold()
    ]
    return PrInfo(
        number=pr["number"],
        title=clean_text(pr.get("title", "")),
        url=pr["url"],
        state=str(pr.get("state", "OPEN")).lower(),
        draft=bool(pr.get("isDraft")),
        build=build_state(pr.get("statusCheckRollup") or []),
        issues=issues,
    )


_ADO_STATE = {"active": "open", "completed": "merged", "abandoned": "closed"}


def ado_build_state(builds: list[dict], has_pending_approval: bool) -> str:
    """Aggregate the latest build of each pipeline (`pr_build.latest_per_pipeline`) into one `BUILD_*`
    value, the way `pr_build.run` (`bdt pr status`) judges them: any failed result is failing, else any
    unfinished build is pending -- or waiting, if a stage is blocked on a manual approval.
    """
    if not builds:
        return BUILD_NONE
    if any(b.get("result") == "failed" for b in builds):
        return BUILD_FAILING
    if any(b.get("status") != "completed" for b in builds):
        return BUILD_WAITING if has_pending_approval else BUILD_PENDING
    return BUILD_PASSING


def _ado_build(session: requests.Session, remote: AdoRemote, pr: dict) -> str:
    pr_id = pr["pullRequestId"]
    source_branch = pr["sourceRefName"].removeprefix("refs/heads/")
    try:
        builds = pr_build.latest_per_pipeline(pr_build.get_builds_for_pr(session, remote, source_branch, pr_id))
        # a timeline lookup per running build -- only worth it when nothing has failed yet
        failed = any(b.get("result") == "failed" for b in builds)
        approval = not failed and any(b.get("status") != "completed" for b in builds) and bool(pr_build.find_pending_approvals(session, remote, builds))
    except requests.RequestException:
        return BUILD_UNKNOWN
    return ado_build_state(builds, approval)


def ado_info(session: requests.Session, pr: dict, remote: AdoRemote) -> PrInfo:
    """`pr` is an Azure DevOps pull request REST object (see `pr_build.get_pr`)."""
    issues = [
        IssueRef(n, ado_issue.edit_url(remote, n))
        for n in pr_issue_link.find_issue_refs_in_body(pr.get("description"), remote)
    ]
    pr_id = pr["pullRequestId"]
    return PrInfo(
        number=pr_id,
        title=clean_text(pr.get("title", "")),
        url=pr_build.pr_web_url(remote, pr_id),
        state=_ADO_STATE.get(str(pr.get("status", "active")).lower(), "open"),
        draft=bool(pr.get("isDraft")),
        build=_ado_build(session, remote, pr),
        issues=issues,
    )
