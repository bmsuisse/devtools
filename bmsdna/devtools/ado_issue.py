"""Azure DevOps work items ("issues"): create/update/delete, comment
add/update/delete, and screenshot support.

Built directly on the Work Item Tracking REST API (api-version 7.1):
  - Create:      POST    {org}/{project}/_apis/wit/workitems/${type}
  - Update:      PATCH   {org}/{project}/_apis/wit/workitems/{id}
  - Delete:      DELETE  {org}/{project}/_apis/wit/workitems/{id} (soft-delete to the Recycle Bin — recoverable;
                 there is no `destroy=true` support here on purpose, since that's a permanent, unrecoverable delete)
  - Get:         GET     {org}/{project}/_apis/wit/workitems/{id}
  - Attachments: POST    {org}/{project}/_apis/wit/attachments?fileName=...
  - Comments:    POST/PATCH/DELETE {org}/{project}/_apis/wit/workItems/{id}/comments[/{commentId}]
                 (api-version 7.1-preview.4 — the "Comments"/discussion resource is still in preview
                 even on the 7.1 line)

Screenshots are handled in two steps, since the classic long-text fields
(Description, Repro Steps, ...) default to HTML formatting via the REST API
(Markdown is an explicit opt-in per field via `/multilineFieldsFormat/...`)
while a raw `![name](url)` would just render as literal text there:
  1. Upload each screenshot as an attachment and link it to the work item via
     an "AttachedFile" relation, so it shows up in the Attachments tab.
  2. Post/append a comment embedding the same images with Markdown `![]()`
     syntax — the work item Discussion/Comments control has rendered
     Markdown (including inline images) since it replaced the old HTML
     System.History field, independent of the Description field's
     HTML/Markdown toggle.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

import requests

from .bdt_config import load_bdt_table
from .gitrepo import AdoRemote
from .pr_markdown import build_screenshots_section

COMMENTS_API_VERSION = "7.1-preview.4"


def resolve_board(board: str | None, start: Path | None = None) -> str | None:
    """--board wins; else `[tool.bdt.ado].board` from pyproject.toml; else None."""
    return board or load_bdt_table("ado", start).get("board")


def _base_url(remote: AdoRemote) -> str:
    return f"https://dev.azure.com/{quote(remote.org, safe='')}/{quote(remote.project, safe='')}"


def get_team_area_path(session: requests.Session, remote: AdoRemote, team: str) -> str:
    """The Area Path new work items need to be filed under to show up on `team`'s board."""
    r = session.get(
        f"{_base_url(remote)}/{quote(team, safe='')}/_apis/work/teamsettings/teamfieldvalues",
        params={"api-version": "7.1"},
    )
    if r.status_code == 404:
        sys.exit(f"Board/team '{team}' not found in project '{remote.project}'. Check --board / [tool.bdt.ado].board in pyproject.toml.")
    r.raise_for_status()
    return r.json()["defaultValue"]


def build_create_ops(
    title: str,
    description: str | None = None,
    area_path: str | None = None,
    tags: list[str] | None = None,
) -> list[dict]:
    """The JSON Patch document body for creating a work item with these fields."""
    ops = [{"op": "add", "path": "/fields/System.Title", "value": title}]
    if description:
        ops.append({"op": "add", "path": "/fields/System.Description", "value": description})
    if area_path:
        ops.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})
    if tags:
        ops.append({"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)})
    return ops


def build_attach_ops(images: list[tuple[str, str]]) -> list[dict]:
    """The JSON Patch document body for linking already-uploaded attachments via 'AttachedFile' relations."""
    return [
        {"op": "add", "path": "/relations/-", "value": {"rel": "AttachedFile", "url": url, "attributes": {"comment": name}}}
        for name, url in images
    ]


def build_update_ops(
    title: str | None = None,
    description: str | None = None,
    area_path: str | None = None,
    tags: list[str] | None = None,
    state: str | None = None,
) -> list[dict]:
    """The JSON Patch document body for updating a work item — only fields that aren't None are touched."""
    ops = []
    if title is not None:
        ops.append({"op": "add", "path": "/fields/System.Title", "value": title})
    if description is not None:
        ops.append({"op": "add", "path": "/fields/System.Description", "value": description})
    if area_path is not None:
        ops.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})
    if tags is not None:
        ops.append({"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)})
    if state is not None:
        ops.append({"op": "add", "path": "/fields/System.State", "value": state})
    return ops


def create_work_item(
    session: requests.Session,
    remote: AdoRemote,
    work_item_type: str,
    title: str,
    description: str | None = None,
    area_path: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    ops = build_create_ops(title, description, area_path, tags)

    r = session.post(
        f"{_base_url(remote)}/_apis/wit/workitems/${quote(work_item_type, safe='')}",
        params={"api-version": "7.1"},
        json=ops,
        headers={"Content-Type": "application/json-patch+json"},
    )
    if r.status_code == 404:
        sys.exit(f"Work item type '{work_item_type}' not found in project '{remote.project}'. Check --type.")
    r.raise_for_status()
    return r.json()


def update_work_item(
    session: requests.Session,
    remote: AdoRemote,
    work_item_id: int,
    title: str | None = None,
    description: str | None = None,
    area_path: str | None = None,
    tags: list[str] | None = None,
    state: str | None = None,
) -> dict:
    ops = build_update_ops(title, description, area_path, tags, state)
    if not ops:
        sys.exit("Nothing to update — provide at least one of --title, --description, --board, --tag, --state.")

    r = session.patch(
        f"{_base_url(remote)}/_apis/wit/workitems/{work_item_id}",
        params={"api-version": "7.1"},
        json=ops,
        headers={"Content-Type": "application/json-patch+json"},
    )
    if r.status_code == 404:
        sys.exit(f"Work item #{work_item_id} not found in project '{remote.project}'.")
    r.raise_for_status()
    return r.json()


def get_work_item_type(session: requests.Session, remote: AdoRemote, work_item_id: int) -> str:
    r = session.get(
        f"{_base_url(remote)}/_apis/wit/workitems/{work_item_id}",
        params={"fields": "System.WorkItemType", "api-version": "7.1"},
    )
    if r.status_code == 404:
        sys.exit(f"Work item #{work_item_id} not found in project '{remote.project}'.")
    r.raise_for_status()
    return r.json()["fields"]["System.WorkItemType"]


def get_valid_states(session: requests.Session, remote: AdoRemote, work_item_type: str) -> list[str]:
    """The state names defined for `work_item_type` by this project's process template —
    state names (and which ones are terminal) are per-type, per-template, not a fixed set.
    """
    r = session.get(
        f"{_base_url(remote)}/_apis/wit/workitemtypes/{quote(work_item_type, safe='')}/states",
        params={"api-version": "7.1"},
    )
    r.raise_for_status()
    return [s["name"] for s in r.json()["value"]]


def delete_work_item(session: requests.Session, remote: AdoRemote, work_item_id: int) -> None:
    """Soft-delete: moves the work item to the project's Recycle Bin, where it can be restored.

    Deliberately doesn't expose the REST API's `destroy=true` option — that's
    a permanent, unrecoverable delete, and there's no confirmation step that
    makes that safe to offer from a CLI flag.
    """
    r = session.delete(f"{_base_url(remote)}/_apis/wit/workitems/{work_item_id}", params={"api-version": "7.1"})
    if r.status_code == 404:
        sys.exit(f"Work item #{work_item_id} not found in project '{remote.project}'.")
    r.raise_for_status()


def upload_attachment(session: requests.Session, remote: AdoRemote, attachment_name: str, file_path: str) -> tuple[str, str]:
    """Upload `file_path` as a work item attachment named `attachment_name`; returns (id, download url)."""
    r = session.post(
        f"{_base_url(remote)}/_apis/wit/attachments",
        params={"fileName": attachment_name, "api-version": "7.1"},
        data=Path(file_path).read_bytes(),
        headers={"Content-Type": "application/octet-stream"},
    )
    r.raise_for_status()
    body = r.json()
    return body["id"], body["url"]


def _upload_screenshots(session: requests.Session, remote: AdoRemote, screenshot_paths: list[str]) -> list[tuple[str, str]]:
    """Upload each screenshot as an attachment; returns (display name, download url) pairs.

    Attachment names are index-prefixed so two screenshots sharing a basename
    (e.g. two 'before.png' from different folders) don't overwrite each other.
    """
    return [
        (Path(path).name, upload_attachment(session, remote, f"{i:02d}-{Path(path).name}", path)[1])
        for i, path in enumerate(screenshot_paths)
    ]


def link_attachments(session: requests.Session, remote: AdoRemote, work_item_id: int, images: list[tuple[str, str]]) -> None:
    """Link already-uploaded attachments to a work item so they show up in its Attachments tab."""
    ops = build_attach_ops(images)
    r = session.patch(
        f"{_base_url(remote)}/_apis/wit/workitems/{work_item_id}",
        params={"api-version": "7.1"},
        json=ops,
        headers={"Content-Type": "application/json-patch+json"},
    )
    r.raise_for_status()


def add_comment(session: requests.Session, remote: AdoRemote, work_item_id: int, text: str) -> dict:
    r = session.post(
        f"{_base_url(remote)}/_apis/wit/workItems/{work_item_id}/comments",
        params={"api-version": COMMENTS_API_VERSION},
        json={"text": text},
    )
    r.raise_for_status()
    return r.json()


def update_comment(session: requests.Session, remote: AdoRemote, work_item_id: int, comment_id: int, text: str) -> dict:
    r = session.patch(
        f"{_base_url(remote)}/_apis/wit/workItems/{work_item_id}/comments/{comment_id}",
        params={"api-version": COMMENTS_API_VERSION},
        json={"text": text},
    )
    if r.status_code == 404:
        sys.exit(f"Comment #{comment_id} not found on work item #{work_item_id}.")
    r.raise_for_status()
    print(f"Updated comment #{comment_id} on work item #{work_item_id}")
    return r.json()


def delete_comment(session: requests.Session, remote: AdoRemote, work_item_id: int, comment_id: int) -> None:
    r = session.delete(
        f"{_base_url(remote)}/_apis/wit/workItems/{work_item_id}/comments/{comment_id}",
        params={"api-version": COMMENTS_API_VERSION},
    )
    if r.status_code == 404:
        sys.exit(f"Comment #{comment_id} not found on work item #{work_item_id}.")
    r.raise_for_status()
    print(f"Deleted comment #{comment_id} on work item #{work_item_id}")


def add_screenshots(session: requests.Session, remote: AdoRemote, work_item_id: int, screenshot_paths: list[str]) -> None:
    """Upload+link screenshots as attachments, then post a comment embedding them (Markdown)."""
    images = _upload_screenshots(session, remote, screenshot_paths)
    link_attachments(session, remote, work_item_id, images)
    comment = add_comment(session, remote, work_item_id, build_screenshots_section(None, images).strip())
    print(f"Attached {len(screenshot_paths)} screenshot(s) to work item #{work_item_id} (comment #{comment['id']})")


def comment_with_screenshots(session: requests.Session, remote: AdoRemote, work_item_id: int, message: str | None, screenshot_paths: list[str]) -> dict:
    """Post a comment, with a message and/or screenshots, on the work item."""
    images = _upload_screenshots(session, remote, screenshot_paths) if screenshot_paths else []
    if images:
        link_attachments(session, remote, work_item_id, images)
    content = build_screenshots_section(message, images).strip() if images else (message or "")
    comment = add_comment(session, remote, work_item_id, content)
    print(f"Added comment #{comment['id']} ({len(screenshot_paths)} screenshot(s)) to work item #{work_item_id}")
    return comment


def html_url(work_item: dict) -> str | None:
    return work_item.get("_links", {}).get("html", {}).get("href")


def edit_url(remote: AdoRemote, work_item_id: int) -> str:
    return f"{_base_url(remote)}/_workitems/edit/{work_item_id}"


def _escape_wiql_string(value: str) -> str:
    return value.replace("'", "''")


# The Completed/Removed-category state names across Azure DevOps's built-in process templates:
# Agile and CMMI use 'Closed' for Completed, Scrum and Basic use 'Done'; all four use 'Removed'.
# There's no single field/value that means "terminal" independent of process template, so this
# union is the closest thing to a default that works out of the box on any of them.
_TERMINAL_STATES = ("Closed", "Done", "Removed")


def build_search_wiql(
    keywords: list[str],
    since: str | None = None,
    area_path: str | None = None,
    state: str = "open",
) -> str:
    """WIQL for work items whose Title or Description contains every given keyword (ANDed),
    optionally restricted to items changed on/after `since` (an ISO 'YYYY-MM-DD' date) and/or
    scoped to a board's Area Path subtree, most recently changed first. `keywords` may be empty,
    to list work items without a text filter.

    `since` is rendered as a UTC ISO 8601 literal (`'YYYY-MM-DDT00:00:00Z'`) — the one
    DateTime format WIQL accepts regardless of the querying account's locale/date-pattern
    preference (a bare `'YYYY-MM-DD'` is parsed using that locale's date pattern instead).

    `state` is `"open"` (default, excludes `_TERMINAL_STATES`), `"closed"` (only those), or
    `"all"` (no state filter). Azure DevOps state names are process-template-specific — Agile and
    CMMI use 'Closed' for their Completed-category state, Scrum and Basic use 'Done' — so
    `_TERMINAL_STATES` is the union across the built-in templates, not a per-project source of
    truth (a custom process with its own state names won't be filtered correctly).
    """
    clauses = [
        f"([System.Title] Contains Words '{_escape_wiql_string(k)}' OR [System.Description] Contains Words '{_escape_wiql_string(k)}')"
        for k in keywords
    ]
    if since:
        clauses.append(f"[System.ChangedDate] >= '{since}T00:00:00Z'")
    if area_path:
        clauses.append(f"[System.AreaPath] UNDER '{_escape_wiql_string(area_path)}'")
    if state == "open":
        clauses.append(" AND ".join(f"[System.State] <> '{s}'" for s in _TERMINAL_STATES))
    elif state == "closed":
        clauses.append("(" + " OR ".join(f"[System.State] = '{s}'" for s in _TERMINAL_STATES) + ")")
    where = " AND ".join(["[System.TeamProject] = @project", *clauses])
    return f"SELECT [System.Id] FROM WorkItems WHERE {where} ORDER BY [System.ChangedDate] DESC"


def run_wiql(session: requests.Session, remote: AdoRemote, wiql: str, top: int) -> list[int]:
    r = session.post(
        f"{_base_url(remote)}/_apis/wit/wiql",
        params={"api-version": "7.1", "$top": top},
        json={"query": wiql},
    )
    r.raise_for_status()
    return [wi["id"] for wi in r.json()["workItems"]]


def get_work_items(session: requests.Session, remote: AdoRemote, ids: list[int]) -> list[dict]:
    """Batch-fetch Title/State for a set of work item ids. WIQL only returns ids, not field values."""
    if not ids:
        return []
    r = session.get(
        f"{_base_url(remote)}/_apis/wit/workitems",
        params={"ids": ",".join(map(str, ids)), "fields": "System.Title,System.State", "api-version": "7.1"},
    )
    r.raise_for_status()
    return r.json()["value"]


def search(
    session: requests.Session,
    remote: AdoRemote,
    keywords: list[str],
    since: str | None = None,
    board: str | None = None,
    top: int = 10,
    state: str = "open",
) -> list[dict]:
    """Search (or, with no keywords, just list) work items by keywords (ANDed, matched against
    Title or Description) and state, most recently changed first. `board` is an Azure Boards team
    name (like `create`'s `--board`) — resolved to its Area Path so results are scoped to that
    team's subtree instead of the whole project.
    """
    area_path = get_team_area_path(session, remote, board) if board else None
    ids = run_wiql(session, remote, build_search_wiql(keywords, since, area_path, state), top)
    items = get_work_items(session, remote, ids)
    for item in items:
        fields = item["fields"]
        print(f"#{item['id']} [{fields['System.State']}] {fields['System.Title']}")
        print(edit_url(remote, item["id"]))
    if not items:
        print("No matching work items found.")
    return items


def create(
    session: requests.Session,
    remote: AdoRemote,
    work_item_type: str,
    title: str,
    description: str | None,
    board: str | None,
    tags: list[str],
    screenshot_paths: list[str],
) -> dict:
    area_path = get_team_area_path(session, remote, board) if board else None
    work_item = create_work_item(session, remote, work_item_type, title, description, area_path, tags)
    work_item_id = work_item["id"]

    print(f"Created {work_item_type} #{work_item_id}: {title}")
    url = html_url(work_item)
    if url:
        print(url)

    if screenshot_paths:
        add_screenshots(session, remote, work_item_id, screenshot_paths)

    return work_item


def update(
    session: requests.Session,
    remote: AdoRemote,
    work_item_id: int,
    title: str | None = None,
    description: str | None = None,
    board: str | None = None,
    tags: list[str] | None = None,
    state: str | None = None,
) -> dict | None:
    """Update a work item's fields. Unlike `create`, `board` is only resolved (and the Area Path only
    touched) when explicitly given — it never falls back to `[tool.bdt.ado].board`, so an unrelated
    field update (e.g. just `--title`) can't silently move the item to a different team's board.

    If `state` isn't one of this work item's type's valid states (state names, and which ones are
    terminal, are defined per work item type per process template — e.g. a Basic-process Issue has
    'Done' but no 'Closed'), the state is left unchanged and a comment records the exact state that
    was requested instead of the update failing or silently doing nothing.
    """
    area_path = get_team_area_path(session, remote, board) if board else None
    applied_state = state
    if state is not None:
        work_item_type = get_work_item_type(session, remote, work_item_id)
        valid_states = get_valid_states(session, remote, work_item_type)
        if not any(state.lower() == s.lower() for s in valid_states):
            add_comment(
                session,
                remote,
                work_item_id,
                f"Requested state change to '{state}', which isn't a valid state for a "
                f"'{work_item_type}' here (valid states: {', '.join(valid_states)}) — left unchanged.",
            )
            applied_state = None

    if title is None and description is None and area_path is None and tags is None and applied_state is None:
        if state is not None:
            print(f"Work item #{work_item_id}: '{state}' isn't a valid state here — noted in a comment.")
            return None
        sys.exit("Nothing to update — provide at least one of --title, --description, --board, --tag, --state.")

    work_item = update_work_item(session, remote, work_item_id, title, description, area_path, tags, applied_state)
    print(f"Updated work item #{work_item_id}")
    return work_item


def delete(session: requests.Session, remote: AdoRemote, work_item_id: int) -> None:
    delete_work_item(session, remote, work_item_id)
    print(f"Deleted work item #{work_item_id} (moved to the Recycle Bin — restorable)")
