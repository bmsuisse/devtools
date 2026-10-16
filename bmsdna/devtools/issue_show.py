"""`bdt issue show <number>`: print one issue / work item -- its body, state, tags, links and comments.

Backend-agnostic: `gh_issue.get_details` / `ado_issue.get_details` fetch the item into an
`IssueDetails`, this module only prints it (as text, or as one JSON document).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .issue_watch import Comment, format_comment


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


def _refs(numbers: list[int]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def format_details(details: IssueDetails, *, as_json: bool = False) -> str:
    """The issue as one JSON document (`as_json`) or as readable text."""
    if as_json:
        return json.dumps(asdict(details), ensure_ascii=False, indent=2)
    lines = [f"#{details.number} [{details.state}] {details.title}", details.url]
    lines += [f"{name}: {value}" for name, value in details.fields.items() if value]
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
    return "\n".join(lines)
