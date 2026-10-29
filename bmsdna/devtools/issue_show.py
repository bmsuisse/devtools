"""`bdt issue show <number>`: print one issue / work item -- its body, state, tags, links and comments.

Backend-agnostic: `gh_issue.get_details` / `ado_issue.get_details` fetch the item into an
`IssueDetails`, this module only prints it (as text, or as one JSON document).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from .issue_watch import Comment, format_comment

# Control characters other than newline and tab: issue text is written by anyone, and a raw ESC sequence
# printed to a terminal can rewrite what it shows (or trigger terminal features).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class IssueDetails:
    number: int
    title: str
    state: str
    url: str
    body: str
    tags: list[str]
    # Backend-specific attributes (Azure DevOps: Type, Area, Iteration, ...); empty values are left out when printing
    fields: dict[str, str | None] = field(default_factory=dict)
    parent: int | None = None
    children: list[int] = field(default_factory=list)
    blocked_by: list[int] = field(default_factory=list)
    blocks: list[int] = field(default_factory=list)
    related: list[int] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    # Azure DevOps with a business-user check: who created it, and whether that is a business user (None = unknown)
    created_by: str | None = None
    business_user: bool | None = None
    business_user_reason: str | None = None


def _refs(numbers: list[int]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def _creator_note(details: IssueDetails) -> str:
    if details.business_user_reason is None:
        return ""
    kind = {True: "business user", False: "developer", None: "unknown"}[details.business_user]
    return f" ({kind}: {details.business_user_reason})"


def format_details(details: IssueDetails, *, as_json: bool = False) -> str:
    """The issue as one JSON document (`as_json`, values unchanged) or as readable text (control characters shown escaped)."""
    if as_json:
        return json.dumps(asdict(details), ensure_ascii=False, indent=2)
    lines = [f"#{details.number} [{details.state}] {details.title}", details.url]
    lines += [f"{name}: {value}" for name, value in details.fields.items() if value]
    if details.created_by:
        lines.append(f"Created by: {details.created_by}{_creator_note(details)}")
    if details.tags:
        lines.append(f"Tags: {', '.join(details.tags)}")
    if details.parent is not None:
        lines.append(f"Parent: #{details.parent}")
    for label, numbers in (
        ("Children", details.children),
        ("Blocked by", details.blocked_by),
        ("Blocks", details.blocks),
        ("Related", details.related),
    ):
        if numbers:
            lines.append(f"{label}: {_refs(numbers)}")
    lines += ["", details.body or "(no description)"]
    if details.comments:
        lines += ["", f"Comments ({len(details.comments)}):"]
        for comment in details.comments:
            lines += ["", format_comment(comment)]
    return _CONTROL_CHARS_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", "\n".join(lines))
