"""Merge defaults of `bdt pr create`: squash and delete the source branch -- but only for PRs from a feature branch.

A PR whose source is a long-lived branch (a promotion `dev` -> `test` -> `main`, or a back-merge `main` -> `dev`)
must keep its commits (squashing leaves the same content in different commits, so every later promotion conflicts
again) and must never delete its source branch. `[tool.bdt.pr].long_lived_branches` in pyproject.toml names those
branches (default: dev, develop, test, main):

    [tool.bdt.pr]
    long_lived_branches = ["dev", "test", "main"]

An explicit `--squash` / `--delete-source-branch` (also `--squash false`) always wins over these defaults.
"""

from __future__ import annotations

from pathlib import Path

from .bdt_config import load_bdt_table
from .pr_create_args import Host, PrCreateOptions

DEFAULT_LONG_LIVED_BRANCHES = ("dev", "develop", "test", "main")


def long_lived_branches(start: Path | None = None) -> tuple[str, ...]:
    """`[tool.bdt.pr].long_lived_branches`, or the defaults when it is absent or not a list of names."""
    raw = load_bdt_table("pr", start).get("long_lived_branches")
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        return DEFAULT_LONG_LIVED_BRANCHES
    return tuple(name.removeprefix("refs/heads/") for name in raw)


def apply_merge_defaults(options: PrCreateOptions, host: Host, source_branch: str, long_lived: tuple[str, ...]) -> None:
    """Default `options.squash` / `options.delete_source_branch` to true unless `source_branch` is long-lived.

    Only what the host can act on is defaulted, so nothing prints an "ignored" note for a flag nobody passed: Azure
    DevOps takes both as completion options; GitHub has no per-PR delete-branch switch and only uses `squash` to pick
    the method of an auto-merge.
    """
    # Azure DevOps resolves branch names case-insensitively, so `--head Dev` is still `dev`
    if source_branch.lower() in {name.lower() for name in long_lived}:
        return
    if options.squash is None and (host == "ado" or options.auto_complete):
        options.squash = True
    if options.delete_source_branch is None and host == "ado":
        options.delete_source_branch = True
