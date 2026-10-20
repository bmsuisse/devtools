"""Backend-agnostic passthrough flags of `bdt pr create` (the args after `--`).

Accepts the union of `gh pr create` and `az repos pr create` spellings and maps them to canonical
`PrCreateOptions` fields; each backend honours what it can and reports the rest (see `unsupported_notes`).
Short flags that mean different things per CLI are resolved with the `host` argument: `-d` is `--draft` on
GitHub but `--description` on Azure DevOps, `-r` is `--reviewer` on GitHub but `--repository` on Azure DevOps, and
`--project` / `-p` is the Projects board on GitHub but the project override on Azure DevOps.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Host = Literal["github", "ado"]

_TRUE = {"true", "yes", "1", "on", "y"}
_FALSE = {"false", "no", "0", "off", "n"}


class PrCreateArgsError(ValueError):
    """An unusable `pr create` passthrough argument (the CLI turns it into a usage error)."""


@dataclass
class PrCreateOptions:
    title: str | None = None
    body: str | None = None
    body_file: str | None = None
    draft: bool | None = None
    base: str | None = None
    head: str | None = None
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)
    required_reviewers: list[str] = field(default_factory=list)
    milestone: str | None = None
    project: str | None = None  # GitHub Projects board (title); Azure DevOps uses `ado_project`
    work_items: list[str] = field(default_factory=list)  # same refs as `bdt pr create --issue`
    squash: bool | None = None
    delete_source_branch: bool | None = None
    auto_complete: bool | None = None
    merge_commit_message: str | None = None
    bypass_policy: bool | None = None
    bypass_policy_reason: str | None = None
    transition_work_items: bool | None = None
    fill: bool | None = None
    fill_first: bool | None = None
    fill_verbose: bool | None = None
    no_maintainer_edit: bool | None = None
    web: bool | None = None
    dry_run: bool | None = None
    organization: str | None = None  # Azure DevOps overrides of the remote-derived values
    ado_project: str | None = None
    repository: str | None = None
    notes: list[str] = field(default_factory=list)  # accepted-but-ignored flags (--output, --query, ...)


# flag -> (kind, field). Kinds: "bool" (optional true/false value), "text" (values up to the next option, joined with a
# space), "lines" (joined one per line), "list" (values up to the next option, comma-split), "ignore", "note".
_COMMON: dict[str, tuple[str, str]] = {
    "--title": ("text", "title"),
    "-t": ("text", "title"),
    "--body": ("lines", "body"),
    "-b": ("lines", "body"),
    "--description": ("lines", "body"),
    "--body-file": ("text", "body_file"),
    "-F": ("text", "body_file"),
    "--draft": ("bool", "draft"),
    "--base": ("text", "base"),
    "-B": ("text", "base"),
    "--target-branch": ("text", "base"),
    "--head": ("text", "head"),
    "-H": ("text", "head"),
    "--source-branch": ("text", "head"),
    "-s": ("text", "head"),
    "--label": ("list", "labels"),
    "--labels": ("list", "labels"),
    "-l": ("list", "labels"),
    "--assignee": ("list", "assignees"),
    "-a": ("list", "assignees"),
    "--reviewer": ("list", "reviewers"),
    "--reviewers": ("list", "reviewers"),
    "--required-reviewers": ("list", "required_reviewers"),
    "--milestone": ("text", "milestone"),
    "-m": ("text", "milestone"),
    "--work-items": ("list", "work_items"),
    "--squash": ("bool", "squash"),
    "--delete-source-branch": ("bool", "delete_source_branch"),
    "--auto-complete": ("bool", "auto_complete"),
    "--auto-merge": ("bool", "auto_complete"),
    "--merge-commit-message": ("text", "merge_commit_message"),
    "--bypass-policy": ("bool", "bypass_policy"),
    "--bypass-policy-reason": ("text", "bypass_policy_reason"),
    "--transition-work-items": ("bool", "transition_work_items"),
    "--fill": ("bool", "fill"),
    "-f": ("bool", "fill"),
    "--fill-first": ("bool", "fill_first"),
    "--fill-verbose": ("bool", "fill_verbose"),
    "--no-maintainer-edit": ("bool", "no_maintainer_edit"),
    "--web": ("bool", "web"),
    "-w": ("bool", "web"),
    "--open": ("bool", "web"),
    "--dry-run": ("bool", "dry_run"),
    "--organization": ("text", "organization"),
    "--org": ("text", "organization"),
    "--detect": ("ignore", ""),
    "--verbose": ("ignore", ""),
    "--debug": ("ignore", ""),
    "--subscription": ("ignore", ""),
    "--output": ("note", ""),
    "-o": ("note", ""),
    "--query": ("note", ""),
}

_HOST_SPECIFIC: dict[Host, dict[str, tuple[str, str]]] = {
    "github": {
        "-d": ("bool", "draft"),
        "-r": ("list", "reviewers"),
        "--project": ("text", "project"),
        "-p": ("text", "project"),
    },
    "ado": {
        "-d": ("lines", "body"),
        "-r": ("text", "repository"),
        "--repository": ("text", "repository"),
        "--project": ("text", "ado_project"),
        "-p": ("text", "ado_project"),
    },
}


def supported_flags(host: Host) -> str:
    flags = {**_COMMON, **_HOST_SPECIFIC[host]}
    return ", ".join(sorted(f for f in flags if f.startswith("--")))


def _is_option(token: str) -> bool:
    return token.startswith("-") and len(token) > 1


def _is_known_flag(token: str, flags: dict[str, tuple[str, str]]) -> bool:
    return _is_option(token) and token.partition("=")[0] in flags


_LABEL_FLAGS = ("--label", "--labels", "-l")


def extract_labels(args: list[str]) -> list[str]:
    """The labels given via `--label` / `--labels` / `-l` in `args`, without validating anything else -- the
    spellings are the same on every host, so the required-label-group check can run before the remote is known."""
    labels: list[str] = []
    i = 0
    while i < len(args):
        name, eq, inline = args[i].partition("=")
        i += 1
        if name not in _LABEL_FLAGS:
            continue
        values = [inline] if eq else []
        while not eq and i < len(args) and not _is_option(args[i]):
            values.append(args[i])
            i += 1
        labels.extend(v.strip() for value in values for v in value.split(",") if v.strip())
    return labels


def parse_pr_create_args(args: list[str], host: Host) -> PrCreateOptions:
    """Parse `args` (`--opt value` and `--opt=value`) for `host`. List-like flags take every following value up to
    the next option (like `az`) and split on commas; a boolean flag takes an optional `true`/`false`. Raises
    `PrCreateArgsError` for an unknown flag, a stray value, a flag without its value or a bad boolean.
    """
    flags = {**_COMMON, **_HOST_SPECIFIC[host]}
    opts = PrCreateOptions()
    i = 0
    while i < len(args):
        token = args[i]
        name, eq, inline = token.partition("=")
        if not _is_option(token) or name not in flags:
            what = "flag" if _is_option(token) else "argument"
            raise PrCreateArgsError(f"unsupported {what} for `pr create` on {_host_name(host)}: {token!r}. Supported flags: {supported_flags(host)}")
        kind, attr = flags[name]
        i += 1
        if kind == "bool":
            if eq:
                raw: str | None = inline
            elif i < len(args) and args[i].lower() in _TRUE | _FALSE:
                raw = args[i]
                i += 1
            else:
                raw = None
            if raw is not None and raw.lower() not in _TRUE | _FALSE:
                raise PrCreateArgsError(f"{name} expects true or false, got {raw!r}")
            setattr(opts, attr, True if raw is None else raw.lower() in _TRUE)
            continue
        if eq:
            values = [inline]
        else:
            values = []
            # A text-like option's first value may start with '-' (`--body "- first change"`): only a token that is
            # exactly a known flag counts as a missing value. Further values stop at any option-like token.
            if kind in ("text", "lines") and i < len(args) and not _is_known_flag(args[i], flags):
                values.append(args[i])
                i += 1
            while i < len(args) and not _is_option(args[i]):
                values.append(args[i])
                i += 1
        if kind == "ignore":
            continue
        if kind == "note":
            opts.notes.append(f"{name} is ignored: bdt prints its own summary")
            continue
        if not values or (kind != "lines" and not any(values)):
            raise PrCreateArgsError(f"{name} needs a value")
        if kind == "list":
            getattr(opts, attr).extend(v.strip() for value in values for v in value.split(",") if v.strip())
        elif kind == "lines":
            setattr(opts, attr, "\n".join(values))
        else:
            setattr(opts, attr, " ".join(values))
    if opts.organization:
        opts.organization = opts.organization.rstrip("/").rsplit("/", 1)[-1]
    for attr in ("base", "head"):
        value = getattr(opts, attr)
        if value:
            setattr(opts, attr, value.removeprefix("refs/heads/"))
    return opts


def _host_name(host: Host) -> str:
    return "GitHub" if host == "github" else "Azure DevOps"


def resolve_body(opts: PrCreateOptions) -> str | None:
    """The PR body: `body`, or the contents of `body_file` (`-` = stdin); both at once is an error (like `gh`)."""
    if opts.body is not None and opts.body_file is not None:
        raise PrCreateArgsError("pass either --body/--description or --body-file, not both")
    if opts.body_file is None:
        return opts.body
    if opts.body_file == "-":
        return sys.stdin.read()
    try:
        return Path(opts.body_file).read_text(encoding="utf-8")
    except OSError as e:
        raise PrCreateArgsError(f"cannot read --body-file {opts.body_file!r}: {e}") from e


# Fields (and their flag spelling) a host has no counterpart for. Honouring the rest is each backend's job.
_UNSUPPORTED: dict[Host, tuple[tuple[str, str], ...]] = {
    "ado": (
        ("assignees", "--assignee"),
        ("milestone", "--milestone"),
        ("project", "--project"),
        ("fill", "--fill"),
        ("fill_first", "--fill-first"),
        ("fill_verbose", "--fill-verbose"),
        ("no_maintainer_edit", "--no-maintainer-edit"),
    ),
    "github": (
        ("required_reviewers", "--required-reviewers"),
        ("delete_source_branch", "--delete-source-branch"),
        ("bypass_policy", "--bypass-policy"),
        ("bypass_policy_reason", "--bypass-policy-reason"),
        ("transition_work_items", "--transition-work-items"),
        ("organization", "--organization"),
        ("ado_project", "--project (Azure DevOps)"),
        ("repository", "--repository"),
    ),
}


def unsupported_notes(opts: PrCreateOptions, host: Host) -> list[str]:
    """One line per given flag `host` cannot honour (e.g. '--milestone is ignored on Azure DevOps'), plus the
    parser's own notes. A backend prints these to stderr; nothing is dropped silently."""
    notes = list(opts.notes)
    for attr, flag in _UNSUPPORTED[host]:
        value = getattr(opts, attr)
        if value is not None and value != [] and value is not False:
            notes.append(f"{flag} is ignored on {_host_name(host)}")
    return notes
