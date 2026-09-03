"""Azure DevOps work items ("issues"): create, comment, and screenshot support.

Built directly on the Work Item Tracking REST API (api-version 7.1):
  - Create:      POST   {org}/{project}/_apis/wit/workitems/${type}
  - Update:      PATCH  {org}/{project}/_apis/wit/workitems/{id}
  - Get:         GET    {org}/{project}/_apis/wit/workitems/{id}
  - Attachments: POST   {org}/{project}/_apis/wit/attachments?fileName=...
  - Comments:    POST   {org}/{project}/_apis/wit/workItems/{id}/comments (api-version 7.1-preview.4 — the
                 "Comments"/discussion resource is still in preview even on the 7.1 line)

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


def add_comment(session: requests.Session, remote: AdoRemote, work_item_id: int, text: str) -> None:
    r = session.post(
        f"{_base_url(remote)}/_apis/wit/workItems/{work_item_id}/comments",
        params={"api-version": COMMENTS_API_VERSION},
        json={"text": text},
    )
    r.raise_for_status()


def add_screenshots(session: requests.Session, remote: AdoRemote, work_item_id: int, screenshot_paths: list[str]) -> None:
    """Upload+link screenshots as attachments, then post a comment embedding them (Markdown)."""
    images = _upload_screenshots(session, remote, screenshot_paths)
    link_attachments(session, remote, work_item_id, images)
    add_comment(session, remote, work_item_id, build_screenshots_section(None, images).strip())
    print(f"Attached {len(screenshot_paths)} screenshot(s) to work item #{work_item_id}")


def comment_with_screenshots(session: requests.Session, remote: AdoRemote, work_item_id: int, message: str | None, screenshot_paths: list[str]) -> None:
    """Post a comment, with a message and/or screenshots, on the work item."""
    images = _upload_screenshots(session, remote, screenshot_paths) if screenshot_paths else []
    if images:
        link_attachments(session, remote, work_item_id, images)
    content = build_screenshots_section(message, images).strip() if images else (message or "")
    add_comment(session, remote, work_item_id, content)
    print(f"Added comment ({len(screenshot_paths)} screenshot(s)) to work item #{work_item_id}")


def html_url(work_item: dict) -> str | None:
    return work_item.get("_links", {}).get("html", {}).get("href")


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
