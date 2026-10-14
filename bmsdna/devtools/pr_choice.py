"""Which PR to act on when a branch has several open ones (e.g. one into `dev` and one into `main`).

The one place that decides this, shared by the Azure DevOps and GitHub lookups of "the PR opened
from the current branch" (`pr_build.get_pr`, `gh_pr.resolve_pr_id`): exactly one candidate is used
silently, several are asked about on a terminal, and never guessed at in a non-interactive run.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import typer


@dataclass(frozen=True)
class PrCandidate:
    number: int
    title: str
    target: str
    url: str


def _interactive() -> bool:
    return sys.stdin.isatty()


def choose_pr(source_branch: str, candidates: list[PrCandidate]) -> int:
    """Index in `candidates` (at least one) of the PR to act on.

    With several, asks which one when stdin is a terminal; otherwise (an agent, CI) exits non-zero
    listing them, since silently picking one could publish/comment on the wrong PR.
    """
    if len(candidates) == 1:
        return 0
    listing = [f"  {i}) #{c.number} -> {c.target}: {c.title} ({c.url})" for i, c in enumerate(candidates, 1)]
    header = f"'{source_branch}' has {len(candidates)} open PRs:"
    if not _interactive():
        sys.exit("\n".join([header, *listing, "Pass --pr-id N or --target[-branch] BRANCH to say which one."]))
    typer.echo("\n".join([header, *listing]), err=True)
    while True:
        answer = typer.prompt("Which PR?", type=int, err=True)
        if 1 <= answer <= len(candidates):
            return answer - 1
        typer.echo(f"Enter a number from 1 to {len(candidates)}.", err=True)
