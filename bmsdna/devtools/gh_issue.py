"""GitHub issue create/update/delete and comment add/update/delete over the GitHub REST/GraphQL API.

Screenshot/file support reuses the `pr-assets` orphan-branch trick from
`gh_pr.py` (GitHub has no API for uploading a file straight into an issue
body/comment — only the web UI's drag-and-drop). Each issue gets its own
`issue-<number>` folder there, mirroring how `gh_pr.py` uses one folder per
source branch.

Endpoints used (all through `github_api.GitHubClient`): `POST/PATCH /repos/{o}/{r}/issues`,
`/issues/{n}/labels`, `/issues/{n}/comments` and `PATCH`/`DELETE /issues/comments/{comment_id}`
(issue and PR conversation comments share that namespace, addressed by comment_id alone — the CLI
still asks for an issue number so `bdt issue comment` reads the same for both hosts), the
sub-issue / dependency endpoints (`/issues/{n}/sub_issues`, `/parent`, `/dependencies/blocked_by`,
`/dependencies/blocking`), `GET /search/issues`, and GraphQL for what REST can't do: deleting an
issue, the newest comment, and GitHub Projects (v2) boards.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .bdt_config import load_bdt_table
from .cli_tools import ensure_agent_session_note
from .github_api import GitHubClient
from .gh_args import Flag, open_in_browser, parse_flags, read_body_file
from .gh_pr import push_assets
from .issue_show import IssueDetails
from .issue_watch import to_comments
from .pr_issue_link import PR_AVAILABLE_LABEL
from .pr_markdown import build_attachments_section, build_comment_content, build_screenshots_section

_COMMENT_ID_RE = re.compile(r"#issuecomment-(\d+)")

# Raw pool size to fetch from the issue search before filtering to a board, as a multiple of the
# caller's requested `limit` -- board membership is filtered client-side after the fact (the search
# has no board-scoping of its own), so asking for exactly `limit` up front would usually
# return fewer than that once off-board results are dropped.
_BOARD_SEARCH_OVERFETCH = 10

# Max items to pull back per board when resolving its membership -- generous enough to cover any
# board this is likely to be pointed at.
_BOARD_ITEM_LIMIT = 500

_SORT_TOKEN = "sort:updated-desc"


def _screenshot_images(owner: str, repo: str, key: str, screenshot_paths: list[str]) -> list[tuple[str, str]]:
    urls = push_assets(owner, repo, key, screenshot_paths)
    return list(zip((Path(p).name for p in screenshot_paths), urls))


def _file_links(owner: str, repo: str, key: str, file_paths: list[str]) -> list[tuple[str, str]]:
    urls = push_assets(owner, repo, key, file_paths)
    return list(zip((Path(p).name for p in file_paths), urls))


def parse_issue_number(issue_url: str) -> str:
    """Pull the issue number off an issue URL, e.g.
    'https://github.com/owner/repo/issues/42' -> '42'.
    """
    return issue_url.rstrip("/").rsplit("/", 1)[-1]


def parse_comment_id(comment_url: str) -> str | None:
    """Pull the numeric comment id off a comment URL's '#issuecomment-<id>' fragment."""
    match = _COMMENT_ID_RE.search(comment_url)
    return match.group(1) if match else None


def resolve_board(board: str | None, start: Path | None = None) -> str | None:
    """--board wins; else `[tool.bdt.github].board` from pyproject.toml; else None."""
    return board or load_bdt_table("github", start).get("board")


# -- Projects (v2) boards -----------------------------------------------------

_OWNER_PROJECTS_QUERY = """
query($login: String!, $after: String) {
  repositoryOwner(login: $login) {
    ... on Organization { projectsV2(first: 100, after: $after) { nodes { id title number } pageInfo { hasNextPage endCursor } } }
    ... on User { projectsV2(first: 100, after: $after) { nodes { id title number } pageInfo { hasNextPage endCursor } } }
  }
}
"""

_BOARD_ITEMS_QUERY = """
fragment Items on ProjectV2 {
  items(first: 100, after: $after) {
    nodes { content { __typename ... on Issue { number repository { nameWithOwner } } } }
    pageInfo { hasNextPage endCursor }
  }
}
query($login: String!, $number: Int!, $after: String) {
  repositoryOwner(login: $login) {
    ... on Organization { projectV2(number: $number) { ...Items } }
    ... on User { projectV2(number: $number) { ...Items } }
  }
}
"""

_ADD_TO_BOARD_MUTATION = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}
"""

_DELETE_ISSUE_MUTATION = """
mutation($issue: ID!) { deleteIssue(input: {issueId: $issue}) { clientMutationId } }
"""


def _find_project_number(projects: list[dict], board: str) -> int | None:
    for project in projects:
        if project.get("title", "").casefold() == board.casefold():
            return project.get("number")
    return None


def _list_projects(gh: GitHubClient, owner: str) -> list[dict]:
    """Every Projects (v2) board of `owner` (an org or a user), as `{id, title, number}`."""
    projects: list[dict] = []
    after: str | None = None
    while True:
        data = gh.graphql(_OWNER_PROJECTS_QUERY, login=owner, after=after)
        connection = ((data or {}).get("repositoryOwner") or {}).get("projectsV2") or {}
        projects.extend(connection.get("nodes") or [])
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage") or len(projects) >= _BOARD_ITEM_LIMIT:
            return projects
        after = page.get("endCursor")


def _find_project(gh: GitHubClient, owner: str, board: str) -> dict:
    projects = _list_projects(gh, owner)
    if board.isdigit():
        found = next((p for p in projects if p.get("number") == int(board)), None)
    else:
        number = _find_project_number(projects, board)
        found = next((p for p in projects if p.get("number") == number), None) if number is not None else None
    if found is None:
        sys.exit(f"GitHub Project board '{board}' not found for owner '{owner}'. Pass its number instead, or check the name.")
    return found


def resolve_project_number(gh: GitHubClient, owner: str, board: str) -> int:
    """The GitHub Projects (v2) board `board` (a number or a title) as its project number."""
    if board.isdigit():
        return int(board)
    number = _find_project_number(_list_projects(gh, owner), board)
    if number is None:
        sys.exit(f"GitHub Project board '{board}' not found for owner '{owner}'. Pass its number instead, or check the name.")
    return number


_ISSUE_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")


def _extract_issue_numbers(items: list[dict], owner: str, repo: str) -> set[int]:
    """Issue numbers among a board's items that belong to `owner/repo` -- drops pull requests and
    draft issues (no repo issue number to match against), and issues from other repos on the same
    org-wide board: issue numbers are only unique within a single repo, so matching on the bare
    number alone would treat e.g. some-other-repo#7 as this repo's #7 too.
    """
    numbers = set()
    for item in items:
        if item.get("type") != "Issue":
            continue
        url = item.get("url")
        if not url:
            continue
        match = _ISSUE_URL_RE.search(url)
        if match and match.group(1).casefold() == owner.casefold() and match.group(2).casefold() == repo.casefold():
            numbers.add(int(match.group(3)))
    return numbers


def _board_items(gh: GitHubClient, owner: str, project_number: int) -> list[dict]:
    """A board's items as `{type, url}` (what `_extract_issue_numbers` reads)."""
    items: list[dict] = []
    after: str | None = None
    while True:
        data = gh.graphql(_BOARD_ITEMS_QUERY, login=owner, number=project_number, after=after)
        connection = (((data or {}).get("repositoryOwner") or {}).get("projectV2") or {}).get("items") or {}
        for node in connection.get("nodes") or []:
            content = (node or {}).get("content") or {}
            if content.get("__typename") == "Issue":
                items.append({"type": "Issue", "url": f"https://github.com/{content['repository']['nameWithOwner']}/issues/{content['number']}"})
            else:
                items.append({"type": content.get("__typename") or "DraftIssue"})
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage") or len(items) >= _BOARD_ITEM_LIMIT:
            return items
        after = page.get("endCursor")


def board_issue_numbers(gh: GitHubClient, owner: str, repo: str, project_number: int) -> set[int]:
    """Issue numbers on `owner`'s Projects (v2) board `project_number` that belong to `owner/repo`."""
    return _extract_issue_numbers(_board_items(gh, owner, project_number), owner, repo)


# -- create -------------------------------------------------------------------


_CREATE_FLAGS = {
    "--title": Flag("title"),
    "-t": Flag("title"),
    "--body": Flag("body"),
    "-b": Flag("body"),
    "--body-file": Flag("body_file"),
    "-F": Flag("body_file"),
    "--label": Flag("label", "list"),
    "-l": Flag("label", "list"),
    "--assignee": Flag("assignee", "list"),
    "-a": Flag("assignee", "list"),
    "--milestone": Flag("milestone"),
    "-m": Flag("milestone"),
    "--project": Flag("project", "list"),
    "-p": Flag("project", "list"),
    "--web": Flag("web", "bool"),
    "-w": Flag("web", "bool"),
}


def resolve_assignees(gh: GitHubClient, assignees: list[str]) -> list[str]:
    """`@me` is the authenticated user."""
    return [gh.get("user")["login"] if a == "@me" else a for a in assignees]


def resolve_milestone(gh: GitHubClient, milestone: str) -> int:
    """A milestone given by number or by title, as its number."""
    if milestone.isdigit():
        return int(milestone)
    milestones = gh.paginate(f"{gh.repo_path}/milestones", {"state": "all"})
    match = next((m for m in milestones if m["title"].casefold() == milestone.casefold()), None)
    if match is None:
        sys.exit(f"Milestone '{milestone}' not found.")
    return match["number"]


def add_to_project(gh: GitHubClient, project: dict, node_id: str) -> None:
    """Add an issue / PR (by GraphQL node id) to a Projects (v2) board returned by `_find_project`."""
    gh.graphql(_ADD_TO_BOARD_MUTATION, project=project["id"], content=node_id)


def find_project(gh: GitHubClient, owner: str, board: str) -> dict:
    return _find_project(gh, owner, board)


def _issue_id(gh: GitHubClient, number: int) -> int:
    """The issue's database id (what the sub-issue / dependency endpoints take, not its number)."""
    return gh.get(f"{gh.repo_path}/issues/{number}")["id"]


def _add_sub_issue(gh: GitHubClient, parent: int, child_id: int, *, replace: bool = False) -> None:
    body: dict[str, Any] = {"sub_issue_id": child_id}
    if replace:
        body["replace_parent"] = True
    gh.post(f"{gh.repo_path}/issues/{parent}/sub_issues", body)


def _add_blocked_by(gh: GitHubClient, number: int, blocker: int) -> None:
    gh.post(f"{gh.repo_path}/issues/{number}/dependencies/blocked_by", {"issue_id": _issue_id(gh, blocker)})


def create(
    gh: GitHubClient,
    owner: str,
    repo: str,
    title: str,
    body: str | None,
    labels: list[str],
    screenshot_paths: list[str],
    extra_args: list[str],
    file_paths: list[str] | None = None,
    board: str | None = None,
    parent: int | None = None,
    blocked_by: list[int] | None = None,
) -> None:
    """Create a GitHub issue, then (if any) attach screenshots/files as a follow-up edit.

    `board` adds the issue to that GitHub Projects (v2) board by title or number (looked up before
    the issue is created, so an unknown board fails early). `parent` makes it a sub-issue of that
    issue and `blocked_by` marks it as blocked by those issues. `extra_args` are the `gh issue create` flags
    --title/-t, --body/-b, --body-file/-F, --label/-l, --assignee/-a, --milestone/-m, --project/-p, --web/-w
    (explicit ones override `title`/`body`; labels merge; anything else, e.g. --template/--recover, is
    rejected: there is no `gh` to forward it to).
    """
    file_paths = file_paths or []
    extra = parse_flags(extra_args, _CREATE_FLAGS, "issue create")
    if "body" in extra and "body_file" in extra:
        sys.exit("specify only one of --body or --body-file")
    title = extra.get("title", title)
    if "body_file" in extra:
        body = read_body_file(extra["body_file"])
    body = ensure_agent_session_note(extra.get("body", body), also_check=title)
    labels = list(dict.fromkeys([*labels, *extra.get("label", [])]))
    payload: dict[str, Any] = {"title": title, "body": body or ""}
    if extra.get("assignee"):
        payload["assignees"] = resolve_assignees(gh, extra["assignee"])
    if "milestone" in extra:
        payload["milestone"] = resolve_milestone(gh, extra["milestone"])
    if labels:
        payload["labels"] = labels
    boards = list(dict.fromkeys([*([board] if board else []), *extra.get("project", [])]))
    projects = [_find_project(gh, owner, b) for b in boards]

    created = gh.post(f"repos/{owner}/{repo}/issues", payload)
    url = created["html_url"]
    print(url)
    number = created["number"]

    for project in projects:
        add_to_project(gh, project, created["node_id"])
    if parent is not None:
        _add_sub_issue(gh, parent, created["id"])
    for blocker in dict.fromkeys(blocked_by or []):
        _add_blocked_by(gh, number, blocker)

    if screenshot_paths or file_paths:
        new_body: str = body or ""
        if screenshot_paths:
            new_body = build_screenshots_section(new_body, _screenshot_images(owner, repo, f"issue-{number}", screenshot_paths))
        if file_paths:
            new_body = build_attachments_section(new_body, _file_links(owner, repo, f"issue-{number}", file_paths))
        gh.patch(f"repos/{owner}/{repo}/issues/{number}", {"body": new_body})
        print(f"Attached {len(screenshot_paths)} screenshot(s), {len(file_paths)} file(s) to issue #{number}")
    if extra.get("web"):
        open_in_browser(url)


# -- labels -------------------------------------------------------------------


def _label_exists(gh: GitHubClient, name: str) -> bool:
    r = gh.request("GET", f"{gh.repo_path}/labels/{quote(name, safe='')}", ok=(404,))
    return r.status_code != 404


def ensure_pr_available_label(gh: GitHubClient) -> None:
    """Make sure the repo-wide `pr-available` label exists, creating it if not -- unlike Azure
    DevOps tags (freeform), adding a label GitHub doesn't already know about is rejected
    (see `gh_pr.create`'s docstring). Only creates the label when it's
    actually missing, so an existing custom color/description isn't clobbered.

    Call this once per `pr create` invocation, before any `add_pr_available_label` calls --
    it's the same repo-wide label being checked/created each time, not a per-issue thing, so a
    caller linking several issues in one go should only pay for this lookup once.
    """
    if not _label_exists(gh, PR_AVAILABLE_LABEL):
        gh.post(
            f"{gh.repo_path}/labels",
            {"name": PR_AVAILABLE_LABEL, "color": "0E8A16", "description": "A PR exists that addresses this issue"},
        )


def add_pr_available_label(gh: GitHubClient, number: int) -> None:
    """Label issue `number` `pr-available`. Caller must have already called
    `ensure_pr_available_label` (once, not per-issue) so the label exists on the repo first.
    """
    gh.post(f"{gh.repo_path}/issues/{number}/labels", {"labels": [PR_AVAILABLE_LABEL]})


# -- search -------------------------------------------------------------------


def build_search_query(keywords: list[str], since: str | None) -> str:
    """The issue search query string: keywords ANDed together (GitHub search's
    implicit default), optionally scoped to issues updated on/after `since` (an ISO
    'YYYY-MM-DD' date) via the `updated:` qualifier. Always sorted `updated:desc` — GitHub's
    default search order is text-relevance, not recency, which `search()`'s ordering relies on.
    `keywords` may be empty, to list issues without a text filter.
    """
    parts = [*keywords, _SORT_TOKEN]
    if since:
        parts.append(f"updated:>={since}")
    return " ".join(parts)


def _quote_qualifier(value: str) -> str:
    return f'"{value}"' if re.search(r"\s", value) else value


SEARCH_RESULT_CAP = 1000


def _error_text(r: Any) -> str:
    """The `message` of an error response, '' when it has none."""
    try:
        body = r.json()
    except ValueError:
        return ""
    return str(body.get("message") or "") if isinstance(body, dict) else ""


def _search_issues(gh: GitHubClient, query: str, scope: str, state: str, labels: list[str], limit: int) -> list[dict]:
    """`GET /search/issues` for `query` (from `build_search_query`) within `scope` (`repo:o/r` / `user:o`),
    newest-updated first, as `{number, title, url, state, repository}` (`state` as the API reports it: lowercase)."""
    parts = [part for part in query.split(" ") if part and part != _SORT_TOKEN]
    parts += ["is:issue", scope]
    if state != "all":
        parts.append(f"is:{state}")
    parts += [f"label:{_quote_qualifier(label)}" for label in labels]
    # the search API serves at most the first 1000 results: asking for page 11 is an HTTP 422
    raw = gh.paginate("search/issues", {"q": " ".join(parts), "sort": "updated", "order": "desc"}, items_key="items", limit=min(limit, SEARCH_RESULT_CAP))
    return [
        {
            "number": item["number"],
            "title": item["title"],
            "url": item["html_url"],
            "state": item["state"],
            "repository": {"nameWithOwner": item["repository_url"].split("/repos/", 1)[-1]},
        }
        for item in raw
    ]


def search(
    gh: GitHubClient,
    owner: str,
    repo: str,
    keywords: list[str],
    since: str | None,
    limit: int,
    state: str = "open",
    board: str | None = None,
    org_wide: bool = False,
    labels: list[str] | None = None,
    comment_filter: Callable[[str | None], bool] | None = None,
    scan_limit: int | None = None,
) -> list[dict]:
    """Search (or, with no keywords, just list) issues by state, most recently updated first.

    `state` is `open|closed|all`. `board`
    scopes results to a GitHub Projects (v2) board (by title or number) -- the search has no
    board filter of its own, so this over-fetches a wider raw pool first and filters down to
    `limit` afterward, client-side. `labels` is ANDed (an issue must carry every given label).

    `org_wide` searches every repo `owner` has instead of just `repo` -- mutually exclusive with
    `board`, whose membership is only resolved against `repo`, not every repo under `owner`.

    `comment_filter` keeps only issues whose newest comment (None if it has none) it accepts --
    e.g. `bdt issue search --for-me`. The newest comment isn't part of the search results,
    so it costs one API call per candidate (in result order, stopping once `limit`
    issues matched); the raw pool is over-fetched like for `board`, as the filter drops results.
    `scan_limit` sizes that pool as if it were the `limit` (default: `limit`), for a caller that wants few
    results out of a wide scan (`issue search --take`).

    The output casing of `state` differs between the two modes: org-wide results keep the search
    API's lowercase 'open'/'closed', the current-repo ones are upper-cased ('OPEN'/'CLOSED').
    """
    if org_wide and board:
        sys.exit("--board can't be combined with --org-wide (board membership is only resolved for the current repo).")

    query = build_search_query(keywords, since)
    fetch_limit = (scan_limit or limit) * _BOARD_SEARCH_OVERFETCH if board or comment_filter else limit

    if org_wide:
        items = _search_issues(gh, query, f"user:{owner}", state, labels or [], fetch_limit)
        if comment_filter:
            items = _filter_by_last_comment(gh, items, limit, comment_filter, _org_wide_repo_of)
        items = items[:limit]
        for item in items:
            print(f"{item['repository']['nameWithOwner']}#{item['number']} [{item['state']}] {item['title']}")
            print(item["url"])
        if not items:
            print("No matching issues found.")
        return items

    items = _search_issues(gh, query, f"repo:{owner}/{repo}", state, labels or [], fetch_limit)
    for item in items:
        item["state"] = item["state"].upper()

    if board:
        project_number = resolve_project_number(gh, owner, board)
        allowed = board_issue_numbers(gh, owner, repo, project_number)
        items = [item for item in items if item["number"] in allowed]

    if comment_filter:
        items = _filter_by_last_comment(gh, items, limit, comment_filter, lambda item: (owner, repo))
    items = items[:limit]
    for item in items:
        print(f"#{item['number']} [{item['state']}] {item['title']}")
        print(item["url"])
    if not items:
        print("No matching issues found.")
    return items


def _org_wide_repo_of(item: dict) -> tuple[str, str]:
    owner, _, repo = item["repository"]["nameWithOwner"].partition("/")
    return owner, repo


def _filter_by_last_comment(
    gh: GitHubClient,
    items: list[dict],
    limit: int,
    comment_filter: Callable[[str | None], bool],
    repo_of: Callable[[dict], tuple[str, str]],
) -> list[dict]:
    """The first `limit` of `items` (in order) whose newest comment `comment_filter` accepts."""
    matched: list[dict] = []
    for item in items:
        if len(matched) >= limit:
            break
        owner, repo = repo_of(item)
        if comment_filter(last_comment(gh, owner, repo, item["number"])):
            matched.append(item)
    return matched


# -- update -------------------------------------------------------------------

_DONE_STATE_NAMES = ("closed", "done", "completed")
_REMOVED_STATE_NAMES = ("removed", "not planned", "not_planned", "wontfix", "won't fix")
_OPEN_STATE_NAMES = ("open", "reopened", "reopen")


def _set_state(gh: GitHubClient, number: int, state: str) -> None:
    """GitHub issues only have two states (open/closed) plus, when closed, a `state_reason` of
    'completed' or 'not_planned' — no per-process-template state names like Azure DevOps. Map the
    common terminal-state spellings onto that: 'Closed'/'Done'/'Completed' close as completed
    (GitHub's "done" concept); 'Removed'/'Not Planned'/'Wontfix' close as not planned; 'Open'/
    'Reopened' reopens. Anything else has no GitHub equivalent — leave the issue's state
    unchanged and comment with the exact state that was requested, so it isn't silently dropped.
    """
    normalized = state.strip().lower()
    path = f"{gh.repo_path}/issues/{number}"
    if normalized in _DONE_STATE_NAMES:
        gh.patch(path, {"state": "closed", "state_reason": "completed"})
    elif normalized in _REMOVED_STATE_NAMES:
        gh.patch(path, {"state": "closed", "state_reason": "not_planned"})
    elif normalized in _OPEN_STATE_NAMES:
        gh.patch(path, {"state": "open"})
    else:
        gh.post(
            f"{path}/comments",
            {"body": f"Requested state change to '{state}', which isn't a valid GitHub issue state — left unchanged."},
        )


def _current_parent(gh: GitHubClient, number: int) -> int | None:
    r = gh.request("GET", f"{gh.repo_path}/issues/{number}/parent", ok=(404,))
    return None if r.status_code == 404 else r.json().get("number")


def _link_changes(gh: GitHubClient, number: int, parent: int | None, blocked_by: list[int] | None) -> tuple[int | None, list[int]]:
    """The parent still to set (None if it already is) and the blockers still to add for issue `number`."""
    new_parent = parent if parent is not None and parent != _current_parent(gh, number) else None
    wanted = list(dict.fromkeys(blocked_by or []))
    if wanted:
        already = {i["number"] for i in gh.paginate(f"{gh.repo_path}/issues/{number}/dependencies/blocked_by")}
        wanted = [b for b in wanted if b not in already]
    return new_parent, wanted


def update(
    gh: GitHubClient,
    number: int,
    title: str | None = None,
    body: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    state: str | None = None,
    parent: int | None = None,
    blocked_by: list[int] | None = None,
) -> None:
    """`parent` / `blocked_by` link the issue to its parent issue / the issues blocking it, adding
    only what isn't there yet (see `_link_changes`) -- like Azure DevOps' `add_tag`, so repeating an
    update doesn't send, or fail on, a link that already exists.
    """
    path = f"{gh.repo_path}/issues/{number}"
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if body is not None:
        fields["body"] = ensure_agent_session_note(body, also_check=title) or ""
    has_link_change = parent is not None or bool(blocked_by)
    new_parent: int | None = None
    new_blockers: list[int] = []
    if has_link_change:
        if number == parent or number in (blocked_by or []):
            sys.exit(f"Issue #{number} can't be linked to itself.")
        new_parent, new_blockers = _link_changes(gh, number, parent, blocked_by)
    has_change = bool(fields or add_labels or remove_labels or new_parent is not None or new_blockers)
    if not has_change and state is None:
        if not has_link_change:
            sys.exit(
                "Nothing to update — provide at least one of --title, --description, --label, --remove-label, --state, "
                "--parent, --blocked-by."
            )
        print(f"Issue #{number}: already linked as requested — nothing to do.")
        return

    if fields:
        gh.patch(path, fields)
    if add_labels:
        gh.post(f"{path}/labels", {"labels": add_labels})
    for label in remove_labels or []:
        r = gh.request("DELETE", f"{path}/labels/{quote(label, safe='')}", ok=(404,))
        # a 404 for "label isn't on the issue" is fine; a 404 for the issue itself is not
        if r.status_code == 404 and "label does not exist" not in _error_text(r).lower():
            sys.exit(f"GitHub DELETE {path}/labels/{label} failed (HTTP 404): {_error_text(r) or 'Not Found'}")
    if new_parent is not None:
        _add_sub_issue(gh, new_parent, _issue_id(gh, number), replace=True)
    for blocker in new_blockers:
        _add_blocked_by(gh, number, blocker)
    if state is not None:
        _set_state(gh, number, state)
    print(f"Updated issue #{number}")


def _numbers(items: list[dict] | None) -> list[int]:
    """The issue numbers of a list of issue objects."""
    return [i["number"] for i in items or [] if i.get("number") is not None]


def get_details(gh: GitHubClient, owner: str, repo: str, number: int) -> IssueDetails:
    """What `bdt issue show` prints for an issue: its fields, links (parent / sub-issues / blocked-by /
    blocking) and comments."""
    base = f"repos/{owner}/{repo}/issues/{number}"
    data = gh.get(base)
    parent = gh.request("GET", f"{base}/parent", ok=(404,))
    return IssueDetails(
        number=data["number"],
        title=data["title"],
        state=data["state"].upper(),
        url=data["html_url"],
        body=data.get("body") or "",
        tags=[label["name"] for label in data.get("labels") or []],
        fields={
            "Assignees": ", ".join(a["login"] for a in data.get("assignees") or []),
            "Milestone": (data.get("milestone") or {}).get("title"),
        },
        parent=None if parent.status_code == 404 else parent.json().get("number"),
        children=_numbers(gh.paginate(f"{base}/sub_issues")),
        blocked_by=_numbers(gh.paginate(f"{base}/dependencies/blocked_by")),
        blocks=_numbers(gh.paginate(f"{base}/dependencies/blocking")),
        comments=to_comments(list_comments(gh, owner, repo, number)),
    )


def delete(gh: GitHubClient, number: int) -> None:
    """Permanently deletes the issue — unlike Azure DevOps work items, GitHub has no recycle bin for issues."""
    node_id = gh.get(f"{gh.repo_path}/issues/{number}")["node_id"]
    gh.graphql(_DELETE_ISSUE_MUTATION, issue=node_id)
    print(f"Deleted issue #{number}")


def comment(
    gh: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    message: str | None,
    screenshot_paths: list[str],
    file_paths: list[str] | None = None,
    agent_note: bool = True,
) -> None:
    """Post a comment, with a message and/or screenshots/files, on a GitHub issue.

    `agent_note=False` leaves out the detected agent-session note, for a caller (`bdt issue do`) that
    is not itself the agent and writes the right session into `message` instead.
    """
    file_paths = file_paths or []
    images = _screenshot_images(owner, repo, f"issue-{number}", screenshot_paths) if screenshot_paths else []
    files = _file_links(owner, repo, f"issue-{number}", file_paths) if file_paths else []
    content = build_comment_content(message, images, files) or ""
    if agent_note:
        content = ensure_agent_session_note(content) or ""
    # explicit owner/repo: an --org-wide claim is not in the cwd repo
    created = gh.post(f"repos/{owner}/{repo}/issues/{number}/comments", {"body": content})
    url = created["html_url"]
    comment_id = parse_comment_id(url) or str(created.get("id") or "") or None
    suffix = f" (comment #{comment_id})" if comment_id else ""
    print(f"Added comment ({len(screenshot_paths)} screenshot(s), {len(file_paths)} file(s)) to issue #{number}{suffix}")
    print(url)


_LAST_COMMENT_QUERY = (
    "query($owner: String!, $repo: String!, $number: Int!) { repository(owner: $owner, name: $repo) "
    "{ issue(number: $number) { comments(last: 1) { nodes { body } } } } }"
)


def last_comment(gh: GitHubClient, owner: str, repo: str, number: int) -> str | None:
    """Text of the issue's newest comment, or None if it has none."""
    data = gh.graphql(_LAST_COMMENT_QUERY, owner=owner, repo=repo, number=number)
    nodes = data["repository"]["issue"]["comments"]["nodes"]
    return nodes[-1]["body"] if nodes else None


def update_comment(gh: GitHubClient, owner: str, repo: str, comment_id: str, text: str) -> None:
    gh.patch(f"repos/{owner}/{repo}/issues/comments/{comment_id}", {"body": text})
    print(f"Updated comment #{comment_id}")


def delete_comment(gh: GitHubClient, owner: str, repo: str, comment_id: str) -> None:
    gh.delete(f"repos/{owner}/{repo}/issues/comments/{comment_id}")
    print(f"Deleted comment #{comment_id}")


def list_comments(gh: GitHubClient, owner: str, repo: str, number: int) -> list[dict]:
    """Every comment on the issue, oldest first, as `{id, author, created_at, url, body}`."""
    return [
        {
            "id": c["id"],
            "author": (c.get("user") or {}).get("login") or "ghost",
            "created_at": c["created_at"],
            "url": c["html_url"],
            "body": c.get("body") or "",
        }
        for c in gh.paginate(f"repos/{owner}/{repo}/issues/{number}/comments")
    ]
