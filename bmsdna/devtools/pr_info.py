"""`bdt pr info`: a one-shot, machine-readable summary of the current branch's PR.

The bdt Claude Code mod (`mods/bdt-status`) polls this to show a link to the PR and the
issue/work item it closes, plus the build state, above the prompt. Kept as plain data (no
human-oriented formatting) so any other tool -- a shell prompt, a dashboard -- can consume it too.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import ado_issue, gh_pr, pr_build, pr_issue_link
from .gitrepo import AdoRemote, GitHubRemote

# `build` values: aggregate of every check on the PR. "unknown" is for trackers where bdt
# doesn't (yet) evaluate checks -- Azure DevOps -- as opposed to "none", a PR with no checks at all.
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


def build_state(checks: list[dict]) -> str:
    """Aggregate a GitHub `statusCheckRollup` into one `BUILD_*` value; worst state wins
    (failing > waiting > pending > passing). Skipped checks don't count against a PR.
    """
    buckets = {gh_pr.check_bucket(c) for c in checks}
    if not buckets:
        return BUILD_NONE
    if buckets & {"fail", "cancel"}:
        return BUILD_FAILING
    if "waiting_approval" in buckets:
        return BUILD_WAITING
    if "pending" in buckets:
        return BUILD_PENDING
    return BUILD_PASSING


def github_info(pr: dict, remote: GitHubRemote) -> PrInfo:
    """`pr` is `gh pr view --json` output with `gh_pr.PR_INFO_FIELDS`."""
    issues = [
        IssueRef(n, f"https://github.com/{remote.owner}/{remote.repo}/issues/{n}")
        for n in pr_issue_link.find_issue_refs_in_body(pr.get("body"), remote)
    ]
    return PrInfo(
        number=pr["number"],
        title=pr.get("title", ""),
        url=pr["url"],
        state=str(pr.get("state", "OPEN")).lower(),
        draft=bool(pr.get("isDraft")),
        build=build_state(pr.get("statusCheckRollup") or []),
        issues=issues,
    )


_ADO_STATE = {"active": "open", "completed": "merged", "abandoned": "closed"}


def ado_info(pr: dict, remote: AdoRemote) -> PrInfo:
    """`pr` is an Azure DevOps pull request REST object (see `pr_build.get_pr`)."""
    issues = [
        IssueRef(n, ado_issue.edit_url(remote, n))
        for n in pr_issue_link.find_issue_refs_in_body(pr.get("description"), remote)
    ]
    pr_id = pr["pullRequestId"]
    return PrInfo(
        number=pr_id,
        title=pr.get("title", ""),
        url=pr_build.pr_web_url(remote, pr_id),
        state=_ADO_STATE.get(str(pr.get("status", "active")).lower(), "open"),
        draft=bool(pr.get("isDraft")),
        build=BUILD_UNKNOWN,
        issues=issues,
    )
