"""`bdt pr list`: the pull requests of the current repo, with id, state, name (title) and tags (labels).

Plain data like `pr_info`, so `--json` can feed other tools. Cheap on purpose: no build or merge status per PR
(that is a request per PR; `bdt pr info --pr-id N` has it).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import requests

from bmsdna.devtools import pr_build
from bmsdna.devtools.cli_tools import HTTP_TIMEOUT_SECS
from bmsdna.devtools.github_api import GitHubClient
from bmsdna.devtools.gitrepo import AdoRemote
from bmsdna.devtools.pr_info import clean_text

STATES = ("open", "merged", "closed", "all")

_ADO_STATUS = {"open": "active", "merged": "completed", "closed": "abandoned", "all": "all"}
_ADO_STATE = {"active": "open", "completed": "merged", "abandoned": "closed"}
_SEARCH_QUALIFIER = {"merged": "is:merged", "closed": "is:closed is:unmerged"}


@dataclass(frozen=True)
class PrSummary:
    number: int
    title: str
    state: str  # "open" | "merged" | "closed"
    draft: bool
    labels: list[str] = field(default_factory=list)  # the tags (Azure DevOps' term) / labels (GitHub's)
    url: str = ""

    def to_json_dict(self) -> dict:
        return asdict(self)


def _github_summary(pr: dict) -> PrSummary:
    # a PR of the pulls endpoint has `merged_at` at the top, one of the search API under `pull_request`
    merged = pr.get("merged_at") or (pr.get("pull_request") or {}).get("merged_at")
    return PrSummary(
        number=pr["number"],
        title=clean_text(pr.get("title") or ""),
        state="merged" if merged else str(pr.get("state", "open")).lower(),
        draft=bool(pr.get("draft")),
        labels=[clean_text(label["name"]) for label in pr.get("labels") or []],
        url=pr["html_url"],
    )


def github_list(gh: GitHubClient, state: str = "open", limit: int = 30) -> list[PrSummary]:
    """Newest first. The pulls endpoint can't tell merged from closed, so those two go through search."""
    if state in _SEARCH_QUALIFIER:
        found = gh.paginate(
            "search/issues",
            {"q": f"repo:{gh.owner}/{gh.repo} is:pr {_SEARCH_QUALIFIER[state]}", "sort": "created", "order": "desc"},
            items_key="items",
            limit=limit,
        )
    else:
        found = gh.paginate(
            f"{gh.repo_path}/pulls", {"state": state, "sort": "created", "direction": "desc"}, limit=limit
        )
    return [_github_summary(pr) for pr in found]


def ado_list(session: requests.Session, remote: AdoRemote, state: str = "open", limit: int = 30) -> list[PrSummary]:
    """Newest first."""
    r = session.get(
        f"{pr_build._base_url(remote)}/_apis/git/repositories/{remote.repo}/pullrequests",
        params={"searchCriteria.status": _ADO_STATUS[state], "$top": limit, "api-version": "7.1"},
        timeout=HTTP_TIMEOUT_SECS,
    )
    r.raise_for_status()
    prs = sorted(r.json().get("value", []), key=lambda p: p["pullRequestId"], reverse=True)
    return [
        PrSummary(
            number=p["pullRequestId"],
            title=clean_text(p.get("title") or ""),
            state=_ADO_STATE.get(str(p.get("status", "active")).lower(), "open"),
            draft=bool(p.get("isDraft")),
            # a label removed in the UI stays in the list with `active: false`
            labels=[clean_text(label["name"]) for label in p.get("labels") or [] if label.get("active", True)],
            url=pr_build.pr_web_url(remote, p["pullRequestId"]),
        )
        for p in prs
    ]


def summary_text(pr: PrSummary) -> str:
    state = f"{pr.state} (draft)" if pr.draft else pr.state
    tags = f"  [{', '.join(pr.labels)}]" if pr.labels else ""
    return f"#{pr.number}  {state}  {pr.title}{tags}"
