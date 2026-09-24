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

Also: scope -> label auto-labeling, configured under `[tool.bdt.pr.scope_labels]`:

    [tool.bdt.pr.scope_labels]
    customers = "e2e-customers"
    billing = "e2e-billing"

`bdt pr create` derives the "scope" from the HEAD commit's conventional-commit
subject (`type(scope): description`, e.g. `feat(customers): ...` -> scope
"customers" -- the format `bdt commit` itself expects, see its README example).
If the derived scope has a configured label, that label is added to the PR's
`--label` list automatically (deduplicated against any explicitly-passed
`--label`). Opt-in: with no `[tool.bdt.pr.scope_labels]` table, this is a
no-op and behavior is unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

from .bdt_config import load_bdt_table

# `type(scope): subject` or `type(scope)!: subject` (breaking-change marker) --
# the conventional-commit form `bdt commit` documents, e.g. `feat(customers): ...`.
_CONVENTIONAL_SCOPE_RE = re.compile(r"^\s*[\w.-]+\(([^)]+)\)!?:\s")


def required_label_groups(start: Path | None = None) -> dict[str, list[str]]:
    """`[tool.bdt.pr.required_labels]` -> {group name: [allowed label, ...]}."""
    raw = load_bdt_table("pr", start).get("required_labels", {})
    if not isinstance(raw, dict):
        return {}
    return {name: choices for name, choices in raw.items() if isinstance(choices, list)}


def scope_labels(start: Path | None = None) -> dict[str, str]:
    """`[tool.bdt.pr.scope_labels]` -> {scope: label to auto-apply}."""
    raw = load_bdt_table("pr", start).get("scope_labels", {})
    if not isinstance(raw, dict):
        return {}
    return {name: label for name, label in raw.items() if isinstance(label, str)}


def parse_conventional_scope(subject: str | None) -> str | None:
    """Extract the scope from a conventional-commit-style subject line
    (`type(scope): description` -> "scope"). None if `subject` is empty or
    doesn't have a parenthesized scope (e.g. a bare `fix: description`)."""
    if not subject:
        return None
    match = _CONVENTIONAL_SCOPE_RE.match(subject)
    if not match:
        return None
    scope = match.group(1).strip()
    return scope or None


def label_for_scope(scope_label_map: dict[str, str], scope: str | None) -> str | None:
    """The configured label for `scope`, matched case-insensitively (same
    convention as `missing_label_groups`), or None if `scope` is unset or
    unconfigured."""
    if not scope:
        return None
    lowered = {name.lower(): label for name, label in scope_label_map.items()}
    return lowered.get(scope.lower())


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
