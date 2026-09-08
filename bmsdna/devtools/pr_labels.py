"""Required PR label groups, configured under `[tool.bdt.pr.required_labels]`
in pyproject.toml:

    [tool.bdt.pr.required_labels]
    type = ["bug", "feature", "chore"]
    risk = ["breaking", "non-breaking"]

Each key names a group; its value is the labels that satisfy it. `bdt pr
create` must be given at least one label from every configured group
(checked locally, before talking to GitHub/Azure DevOps, so a PR missing a
required label is never created in the first place). Applies identically on
both hosts.
"""

from __future__ import annotations

from pathlib import Path

from .bdt_config import load_bdt_table


def required_label_groups(start: Path | None = None) -> dict[str, list[str]]:
    """`[tool.bdt.pr.required_labels]` -> {group name: [allowed label, ...]}."""
    raw = load_bdt_table("pr", start).get("required_labels", {})
    if not isinstance(raw, dict):
        return {}
    return {name: choices for name, choices in raw.items() if isinstance(choices, list)}


def missing_label_groups(groups: dict[str, list[str]], labels: list[str]) -> dict[str, list[str]]:
    """The subset of `groups` for which none of `labels` is a member.

    Matched case-insensitively — this is a local config check independent of
    whatever casing rules GitHub/Azure DevOps apply to the label text that
    actually gets sent through unchanged.
    """
    given = {label.lower() for label in labels}
    return {name: choices for name, choices in groups.items() if given.isdisjoint(choice.lower() for choice in choices)}


def format_missing_groups_error(missing: dict[str, list[str]]) -> str:
    parts = [f"'{name}' (choose one of: {', '.join(choices)})" for name, choices in missing.items()]
    return "Missing required PR label(s) for group " + "; group ".join(parts)
