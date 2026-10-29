from __future__ import annotations

import contextlib
import io
import json
import math
import re
import subprocess
import sys
import tomllib
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path

import requests
import typer
from pgdevkit.testdb import constants as pgdevkit_constants

from . import ado_issue, agent_mentions, api_usage as api_usage_mod, app_service_logs, commit as commit_mod, dead_code as dead_code_mod
from . import env_config
from . import env_files as env_files_mod
from . import find_repo as find_repo_mod
from . import issue_do as issue_do_mod
from . import issue_watch as issue_watch_mod
from . import gh_issue, gh_pr
from . import business_user as business_user_mod
from . import issue_show as issue_show_mod
from . import find_injection as find_injection_mod
from . import lint as lint_mod
from . import logs as logs_mod
from . import translate as translate_mod
from . import pr_build, pr_info as pr_info_mod, pr_issue_link, pr_ref, pr_labels, pull as pull_mod, worktree as worktree_mod
from .ado_auth import auth_header
from .bdt_config import find_pyproject, load_bdt_table
from .cli_tools import CLI_TIMEOUT_SECS, EXIT_WAIT_TIMEOUT, detect_agent_session, require_gh
from .github_api import GitHubClient
from .pr_create_args import PrCreateArgsError, extract_labels, parse_pr_create_args, resolve_body, unsupported_notes
from .usage.cli import usage_app
from .gitrepo import (
    AdoRemote,
    GitHubRemote,
    NotAzureDevOpsRemoteError,
    NotGitHubRemoteError,
    UnknownRemoteError,
    current_branch,
    current_remote,
    head_commit_subject,
    origin_url,
    parse_remote,
)
from .lint_findings import render_findings

# Non-ASCII output (checkmarks, en-dashes in ADO project names, etc.) needs a
# UTF-8 stream — the default Windows console codepage isn't UTF-8, and would
# otherwise raise UnicodeEncodeError on the first ✓/✗ printed.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

PACKAGE_NAME = "bmsdna-devtools"
__version__ = _pkg_version(PACKAGE_NAME)

# What `--target` / `--target-branch` mean when not given: the branch PRs normally go into.
DEFAULT_TARGET_BRANCH = "dev"

app = typer.Typer(
    name="bdt",
    help=f"Shared BMS developer tooling: PRs/builds (Azure DevOps or GitHub), worktrees, commits, logs (v{__version__})",
)


UPDATE_HINT = (
    f"Update: uv tool upgrade {PACKAGE_NAME}  (installed globally)\n"
    f"        uv sync --upgrade-package {PACKAGE_NAME}  (as a project dependency)"
)


def _version_callback(value: bool) -> None:
    if value:
        print(f"bdt {__version__}")
        print(UPDATE_HINT)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the bdt version and exit.",
    ),
) -> None:
    pass


pr_app = typer.Typer(name="pr", help="Pull request commands (Azure DevOps or GitHub, auto-detected from the git remote)")
app.add_typer(pr_app, name="pr")

translate_app = typer.Typer(name="translate", help="Generate <lng>.json files from translations.toml")
app.add_typer(translate_app, name="translate")

env_app = typer.Typer(name="env", help="Inspect .env files without exposing their values")
app.add_typer(env_app, name="env")

issue_app = typer.Typer(name="issue", help="Issue / work item commands (Azure DevOps or GitHub, auto-detected from the git remote)")
app.add_typer(issue_app, name="issue")

issue_comment_app = typer.Typer(name="comment", help="Comment on an issue / work item")
issue_app.add_typer(issue_comment_app, name="comment")

logs_app = typer.Typer(name="logs", help="Application Insights / Log Analytics queries")
app.add_typer(logs_app, name="logs")

app.add_typer(usage_app, name="usage")

cleanup_app = typer.Typer(
    name="cleanup",
    help="Prune merged git worktrees and their orphaned pgdevkit Postgres test databases",
)
app.add_typer(cleanup_app, name="cleanup")

# Shared `--pg-port`/`--pg-user` defaults for `bdt cleanup *`: pgdevkit's own
# test-container port/user (its `find_orphaned_dbs()`/`workspace_db_names()`
# connect via *its* PGDEVKIT_TESTDB_* env vars, not these flags -- these only
# drive bdt's own `psql`-based DROP DATABASE step, see testdb.py's module
# docstring -- so defaulting to anything else would make the two halves of
# `bdt cleanup orphaned-dbs` silently target different Postgres instances).
_PG_HOST_OPTION = typer.Option(
    pgdevkit_constants.HOST, "--pg-host", envvar="PGHOST", help="Postgres host to connect to (default: pgdevkit's own test-container host)"
)
_PG_PORT_OPTION = typer.Option(
    pgdevkit_constants.PORT, "--pg-port", envvar="PGPORT", help="Postgres port to connect to (default: pgdevkit's own test-container port)"
)
_PG_USER_OPTION = typer.Option(
    None,
    "--pg-user",
    # Deliberately just PGUSER, not also $USER/$LOGNAME: those generic OS
    # envvars are set on virtually every shell, which would make `pg_user or
    # pgdevkit_constants.USER`'s fallback never fire and silently connect as
    # the wrong role on everyone's machine.
    envvar="PGUSER",
    help="Postgres user to connect as (default: pgdevkit's own test-container user)",
)


def _resolve_ado_pr(pat: str | None, remote: AdoRemote, target: str | None, pr_id: int | None = None) -> tuple[requests.Session, dict]:
    """Resolve the PR to act on -- by `pr_id` directly if given (no branch involved at all,
    so this works even without that PR's branch checked out locally), else by searching for
    the current branch's PR into `target`, as before.
    """
    session = requests.Session()
    session.headers.update(auth_header(pat))
    if pr_id is not None:
        pr = pr_build.get_pr_by_id(session, remote, pr_id)
    else:
        pr = pr_build.get_pr(session, remote, current_branch(), target)
    return session, pr


def _current_pr_info(pr_id: int | None, target_branch: str | None, pat: str | None) -> pr_info_mod.PrInfo:
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh = require_gh()
        return pr_info_mod.github_info(gh_pr.get_pr(gh, gh_pr.resolve_pr_id(gh, pr_id, target_branch), fields=gh_pr.PR_INFO_FIELDS), remote)
    session, pr = _resolve_ado_pr(pat, remote, target_branch, pr_id=pr_id)
    return pr_info_mod.ado_info(session, pr, remote)


def _open_prs_info(target_branch: str | None, pat: str | None) -> list[pr_info_mod.PrInfo]:
    """`_current_pr_info` for every open PR from the current branch, `[]` if there is none."""
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh = require_gh()
        return [
            pr_info_mod.github_info(gh_pr.get_pr(gh, number, fields=gh_pr.PR_INFO_FIELDS), remote)
            for number in gh_pr.open_pr_numbers(gh, target_branch)
        ]
    session = requests.Session()
    session.headers.update(auth_header(pat))
    return [pr_info_mod.ado_info(session, pr, remote) for pr in pr_build.get_active_prs(session, remote, current_branch(), target_branch)]


_REMOTE_ERRORS = (UnknownRemoteError, NotGitHubRemoteError, NotAzureDevOpsRemoteError)


def _current_remote_or_none() -> AdoRemote | GitHubRemote | None:
    """The current repo's remote, or None outside a checkout -- without git's own complaint, as that is no error here."""
    r = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        return None
    try:
        return parse_remote(r.stdout.strip())
    except _REMOTE_ERRORS:
        return None


def _ref_pr_info(ref: pr_ref.PrRef, pat: str | None, clients: dict) -> pr_info_mod.PrInfo:
    """The PR `ref` names, in its own repo (the current one for a bare number). `clients` caches the GitHub client
    and Azure DevOps session across the entries of one call, so the token and auth are looked up once."""
    remote = ref.remote or current_remote()
    if isinstance(remote, GitHubRemote):
        if "gh" not in clients:
            clients["gh"] = require_gh()
        base = clients["gh"]
        gh = base if ref.remote is None else base.for_repo(remote.owner, remote.repo)
        return pr_info_mod.github_info(gh_pr.get_pr(gh, ref.pr_id, fields=gh_pr.PR_INFO_FIELDS), remote)
    if "session" not in clients:
        clients["session"] = requests.Session()
        clients["session"].headers.update(auth_header(pat))
    session = clients["session"]
    return pr_info_mod.ado_info(session, pr_build.get_pr_by_id(session, remote, ref.pr_id), remote)


def _failure_text(e: BaseException) -> str:
    return str(e.code) if isinstance(e, SystemExit) and isinstance(e.code, str) else str(e) or type(e).__name__


def _ref_infos(refs: list[pr_ref.PrRef], pat: str | None) -> tuple[list[pr_info_mod.PrInfo], list[str]]:
    """`_ref_pr_info` for each of `refs`, in order: the PRs found and one message per entry that failed, so one
    unreachable repo or unknown number doesn't hide the others."""
    clients: dict = {}
    infos: list[pr_info_mod.PrInfo] = []
    errors: list[str] = []
    for ref in refs:
        try:
            infos.append(_ref_pr_info(ref, pat, clients))
        except (SystemExit, requests.RequestException, KeyError, *_REMOTE_ERRORS) as e:
            errors.append(f"{ref}: {_failure_text(e)}")
    return infos, errors


def _gh_branch_for(gh: GitHubClient, pr_id: int | None, *, needed: bool) -> str:
    """Branch to namespace uploaded screenshots/files under (see `gh_pr.push_assets`) --
    the checked-out branch normally, or (when acting on a PR by `--pr-id`, which may not
    have its branch checked out locally at all) that PR's actual head branch, via `gh`.

    `needed=False` skips that extra `gh pr view` round-trip entirely when the caller isn't
    attaching anything (a plain message/title/description update needs no branch at all).
    """
    if pr_id is None:
        return current_branch()
    if not needed:
        return ""
    return gh_pr.head_branch(gh, pr_id) or str(pr_id)


_LOOKUP_TARGET_HELP = "Only consider a PR into this target branch (default: any target). Several open PRs for the branch: asks which one (non-interactive: fails listing them) unless this or --pr-id picks one"
_PR_ID_HELP = "Act on this PR by ID/number directly, instead of resolving it from the current git branch (lets you skip having the PR's branch checked out locally)"


def _after_create(step: Callable[[], None], label: str) -> None:
    """Run a follow-up step (attaching screenshots/files, noting the agent session, ...)
    without letting its failure mask an already-successful `pr create`.

    The PR itself is already live by the time this runs; a transient failure
    here (a rejected push, an attachment upload error, a stale --target not
    matching the PR ADO actually created) should surface as a warning, not
    flip the whole command's exit code or hide the fact that the PR exists.
    """
    try:
        step()
    except (Exception, SystemExit) as e:
        print(f"Warning: PR created, but {label} failed: {e}")


def _merge_tags_and_labels(tag: list[str] | None, label: list[str] | None) -> list[str] | None:
    """`--tag` (Azure DevOps' term) and `--label` (GitHub's) are aliases of each other -- each
    should route to whichever backend is actually active instead of being silently ignored by
    the other one. Merges the two (deduplicated, case-insensitively, first occurrence wins) into
    a single list handed to `gh_issue.*`'s `labels`/`add_labels`/`remove_labels` or
    `ado_issue.*`'s `tags`/`remove_tags` params.

    Returns `None` (not `[]`) when both inputs are `None` -- distinct from "explicitly passed as
    empty" -- so `issue update`'s "omit --tag/--label to leave tags unchanged" contract isn't
    broken into "replace tags with an empty list" just because this merge ran.
    """
    if tag is None and label is None:
        return None
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*(tag or []), *(label or [])]:
        if value.casefold() not in seen:
            seen.add(value.casefold())
            merged.append(value)
    return merged


def _reject_ado_only_options(**options: object) -> None:
    """GitHub has no counterpart of these `issue create/update` options (Azure DevOps only): fail up front
    instead of silently dropping them."""
    for name, value in options.items():
        if value:
            flag = "--" + name.replace("_", "-")
            raise typer.BadParameter(f"{flag} is only supported for Azure DevOps work items, not GitHub issues.", param_hint=flag)


def _link_and_label_github(gh: GitHubClient, remote: GitHubRemote, issue_numbers: list[int], pr_id: int | None = None) -> None:
    """Link every issue in `issue_numbers`, plus every issue `find_issue_refs_in_body` finds in
    the PR's actual body, to PR `pr_id` (else the current branch's PR), and label each `pr-available`.

    Best-effort per issue -- one bad/inaccessible issue number shouldn't stop the others from
    being linked/labeled; failures are collected and surfaced together to the caller (which
    wraps this in `_after_create`, so they're reported as a warning, not a failed `pr create`).
    A no-op (no `gh` calls at all) when there's nothing to link.
    """
    pr_number, body = gh_pr.get_pr_body(gh, pr_id)
    all_numbers = list(dict.fromkeys([*issue_numbers, *pr_issue_link.find_issue_refs_in_body(body, remote)]))
    if not all_numbers:
        return
    gh_issue.ensure_pr_available_label(gh)
    errors: list[str] = []
    for number in all_numbers:
        try:
            body = gh_pr.link_issue_to_pr(gh, pr_number, body, number)
            gh_issue.add_pr_available_label(gh, number)
        except SystemExit as e:
            errors.append(f"#{number}: {e}")
    if errors:
        raise RuntimeError("; ".join(errors))


def _link_and_label_ado(session: requests.Session, remote: AdoRemote, pr_id: int, description: str, issue_numbers: list[int]) -> None:
    """Azure DevOps equivalent of `_link_and_label_github`: link every issue in `issue_numbers`,
    plus every work item `find_issue_refs_in_body` finds in the PR's description, to PR `pr_id`,
    and tag each `pr-available`.

    Best-effort per work item -- one bad/inaccessible number shouldn't stop the others from being
    linked/tagged; failures (a REST error, or a 404 `sys.exit` from deeper in `ado_issue.py`) are
    collected and surfaced together to the caller (which wraps this in `_after_create`, so they're
    reported as a warning, not a failed `pr create`).
    """
    all_numbers = list(dict.fromkeys([*issue_numbers, *pr_issue_link.find_issue_refs_in_body(description, remote)]))
    errors: list[str] = []
    for number in all_numbers:
        try:
            pr_build.link_work_item(session, remote, pr_id, number)
            ado_issue.add_pr_available_tag(session, remote, number)
        except (requests.RequestException, SystemExit) as e:
            errors.append(f"#{number}: {e}")
    if errors:
        raise RuntimeError("; ".join(errors))


@pr_app.command("create")
def pr_create(
    target: str = typer.Option(DEFAULT_TARGET_BRANCH, "--target", help="Target branch (e.g. dev, main, test)"),
    draft: bool = typer.Option(
        True,
        "--draft/--no-draft",
        help="Create the PR as a draft (not ready for review). Defaults to draft; pass --no-draft to publish it immediately.",
    ),
    label: list[str] = typer.Option(
        [],
        "--label",
        help="Label to apply to the PR (repeatable). Unknown labels are created automatically on both hosts. "
        r"[tool.bdt.pr.required_labels] in pyproject.toml can require at least one label from each "
        "named group before the PR is created. [tool.bdt.pr.scope_labels] can auto-add a label based "
        "on the HEAD commit's conventional-commit scope (e.g. `feat(customers): ...` -> a configured label).",
    ),
    title: str | None = typer.Option(None, "--title", help="PR title (default: from the commits)"),
    description: str | None = typer.Option(
        None,
        "--description",
        help="PR description, kept in full (default: from the commits)",
    ),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to attach to the PR description (repeatable)"
    ),
    file: list[str] = typer.Option(
        [], "--file", help="Path to an arbitrary file to attach to the PR description as a linked attachment (repeatable)"
    ),
    issue: list[str] = typer.Option(
        [],
        "--issue",
        help="Issue number or URL (GitHub issue or Azure DevOps work item) to link to this PR (repeatable). "
        "Each linked issue/work item is also labeled/tagged 'pr-available'. Independent of this flag, the PR "
        "body is scanned for 'Fixes/Closes/Resolves #NR' and full issue/work-item URLs; every match found "
        "there gets the same link + label/tag treatment automatically.",
    ),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
    args: list[str] = typer.Argument(None, help="Extra `gh pr create` / `az repos pr create` flags after `--` (both spellings work on both hosts): "
        "--title/-t, --body/--description/-b (-d on Azure DevOps), --body-file/-F, --draft, --base/-B/--target-branch, "
        "--head/-H/--source-branch/-s, --label[s]/-l, --reviewer[s] (-r on GitHub), --required-reviewers, --work-items, "
        "--auto-complete/--auto-merge, --squash, --assignee/-a, --milestone/-m, --web/-w/--open, --dry-run, "
        "--delete-source-branch, --merge-commit-message, --bypass-policy[-reason], --transition-work-items, "
        "--organization, --project, --repository. A flag the host cannot honour is reported on stderr"),
) -> None:
    """Create a PR from the current branch into --target (Azure DevOps or GitHub, auto-detected)."""
    for path in screenshot:
        if not Path(path).is_file():
            raise typer.BadParameter(f"Screenshot not found: {path}", param_hint="--screenshot")
    for path in file:
        if not Path(path).is_file():
            raise typer.BadParameter(f"File not found: {path}", param_hint="--file")

    scope_label_map = pr_labels.scope_labels()
    if scope_label_map:
        scope = commit_mod.conventional_commit_scope(head_commit_subject())
        auto_label = pr_labels.label_for_scope(scope_label_map, scope)
        if auto_label and auto_label.lower() not in {existing.lower() for existing in label}:
            print(f"Auto-applying label '{auto_label}' for scope '{scope}' ([tool.bdt.pr.scope_labels])")
            label = [*label, auto_label]

    missing_groups = pr_labels.missing_label_groups(pr_labels.required_label_groups(), [*label, *extract_labels(args or [])])
    if missing_groups:
        raise typer.BadParameter(pr_labels.format_missing_groups_error(missing_groups), param_hint="--label")

    remote = current_remote()

    host = "github" if isinstance(remote, GitHubRemote) else "ado"
    try:
        options = parse_pr_create_args(args or [], host)
        body_text = resolve_body(options)
    except PrCreateArgsError as e:
        raise typer.BadParameter(str(e), param_hint="ARGS") from e
    # Resolved once (`--body-file -` can only be read from stdin once): later steps use `options.body` as is.
    options.body, options.body_file = body_text, None
    for note in unsupported_notes(options, host):
        print(f"Note: {note}", file=sys.stderr)
    title = title if title is not None else options.title
    description = description if description is not None else body_text
    draft = options.draft if options.draft is not None else draft
    label = list(dict.fromkeys([*label, *options.labels]))
    target = options.base or target
    issue = [*issue, *options.work_items]

    issue_numbers: list[int] = []
    for ref in issue:
        try:
            issue_numbers.append(pr_issue_link.parse_issue_ref(ref, remote))
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--issue") from e
    issue_numbers = list(dict.fromkeys(issue_numbers))

    source_branch = options.head or current_branch()

    pr_url: str | None = None
    if isinstance(remote, GitHubRemote):
        gh = require_gh()
        returncode, pr_url = gh_pr.create(gh, target, [], draft=draft, labels=label, title=title, description=description, options=options)
        if options.dry_run:
            # Nothing was created: no attachments, issue links or labels may touch whatever PR the branch already has.
            raise typer.Exit(returncode)
        build_policy = gh_pr.has_build_policy(gh, target)
        # The PR just created, by number: `--head` may name a branch other than the checked-out one.
        created = gh_pr.number_from_url(pr_url)
        if returncode == 0 and (screenshot or file):
            _after_create(lambda: gh_pr.add_attachments(gh, remote.owner, remote.repo, source_branch, screenshot, file, pr_id=created), "attaching screenshots/files")
        if returncode == 0:
            _after_create(lambda: _link_and_label_github(gh, remote, issue_numbers, pr_id=created), "linking issue(s) / setting 'pr-available' label")
    else:
        ado = replace(
            remote,
            **{k: v for k, v in (("org", options.organization), ("project", options.ado_project), ("repo", options.repository)) if v},
        )
        session = requests.Session()
        session.headers.update(auth_header(pat))
        if options.dry_run:
            body = pr_build.build_create_body(
                session, ado, source_branch, target, draft=draft, labels=label, title=title, description=description,
                options=options, work_item_ids=issue_numbers,
            )
            typer.echo(json.dumps(body, indent=2))
            raise typer.Exit(0)
        returncode = 0
        try:
            pr_json = pr_build.create_pr(
                session, ado, source_branch, target, draft=draft, labels=label, title=title, description=description,
                options=options, work_item_ids=issue_numbers,
            )
        except SystemExit as e:
            print(e, file=sys.stderr)
            returncode = 1
        else:
            print(f"PR #{pr_json.get('pullRequestId', '?')}: {pr_json.get('title', '?')}")
            try:
                pr_url = pr_build.pr_web_url(ado, pr_json["pullRequestId"])
            except (KeyError, TypeError):
                pr_url = None
        build_policy = pr_build.has_build_policy(session, ado, target)
        created_id = pr_json.get("pullRequestId") if returncode == 0 else None
        if returncode == 0 and (detect_agent_session() is not None or screenshot or file):
            def _finish() -> None:
                pr = pr_build.get_pr_by_id(session, ado, created_id) if created_id else pr_build.get_pr(session, ado, source_branch, target)
                if screenshot or file:
                    pr = pr_build.add_attachments(session, ado, pr, screenshot, file)
                pr_build.ensure_session_note(session, ado, pr)

            _after_create(_finish, "attaching screenshots/files and/or noting the agent session (retry with `bdt pr update`)")
        if returncode == 0:
            # Fetches the PR fresh via `pr_build.get_pr` rather than trusting `pr_json` -- `az`
            # can exit 0 with stdout that isn't clean JSON (e.g. a deprecation banner ahead of
            # it), which already leaves `pr_json` as None above; gating issue-linking on it too
            # would then silently skip it instead of at least warning, unlike every other
            # best-effort step here.
            def _link_issues() -> None:
                pr = pr_build.get_pr_by_id(session, ado, created_id) if created_id else pr_build.get_pr(session, ado, source_branch, target)
                _link_and_label_ado(session, ado, pr["pullRequestId"], pr.get("description") or "", issue_numbers)

            _after_create(_link_issues, "linking work item(s) / setting 'pr-available' tag")

    if returncode == 0 and pr_url:
        print(f"\n{pr_url}")
        if options.web and not isinstance(remote, GitHubRemote):  # `gh_pr.create` opens GitHub's itself
            webbrowser.open(pr_url)

    if returncode == 0 and draft:
        print("\nCreated as a draft PR. Run `bdt pr publish` to mark it ready for review.")

    if returncode == 0 and build_policy:
        print("\nRun `bdt pr status` to check whether the CI build passes.")

    raise typer.Exit(returncode)


@pr_app.command("publish")
def pr_publish(
    target: str | None = typer.Option(None, "--target", help=_LOOKUP_TARGET_HELP),
    pr_id: int | None = typer.Option(None, "--pr-id", help=_PR_ID_HELP),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Mark the draft PR opened from the current branch (or --pr-id, if given) as ready for
    review (Azure DevOps or GitHub, auto-detected).
    """
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh = require_gh()
        gh_pr.publish(gh, pr_id=gh_pr.resolve_pr_id(gh, pr_id, target))
    else:
        session, pr = _resolve_ado_pr(pat, remote, target, pr_id=pr_id)
        pr_build.publish(session, remote, pr)


@pr_app.command("status")
def pr_status(
    target_branch: str | None = typer.Option(None, "--target-branch", help=_LOOKUP_TARGET_HELP),
    wait: bool = typer.Option(False, "--wait", help="Poll until all pipelines/checks are completed; stops early and reports status if one needs manual approval"),
    timeout: float = typer.Option(
        0,
        "--timeout",
        help="With --wait: stop after this many seconds if the checks are still running, print their current state and exit "
        f"{EXIT_WAIT_TIMEOUT} (0 = wait forever, the default). Use it where the caller's own time limit is shorter than CI, "
        "e.g. an agent's shell tool, then re-run the same command to keep waiting",
    ),
    pr_id: int | None = typer.Option(None, "--pr-id", help=_PR_ID_HELP),
    pat: str | None = typer.Option(None, "--pat", envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"], help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)"),
) -> None:
    """Show build/check status for the PR opened from the current branch, or for --pr-id
    directly (Azure DevOps or GitHub, auto-detected).
    """
    if timeout and not wait:
        raise typer.BadParameter("--timeout only works together with --wait", param_hint="--timeout")
    _check_poll_options(None, timeout)
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh = require_gh()
        gh_pr.run(gh, wait, pr_id=gh_pr.resolve_pr_id(gh, pr_id, target_branch), timeout=timeout)
        return
    pr_build.run(remote, pat, target_branch, wait, pr_id=pr_id, timeout=timeout)


def _info_text(info: pr_info_mod.PrInfo, with_repo: bool = False) -> str:
    flags = ", ".join([info.state, *(["draft"] if info.draft else []), f"build: {info.build}"])
    name = f"PR {info.repo}#{info.number}" if with_repo and info.repo else f"PR #{info.number}"
    return "\n".join([f"{name}: {info.title} ({flags})", info.url, *(f"Issue #{i.number}: {i.url}" for i in info.issues)])


def _echo_infos(infos: list[pr_info_mod.PrInfo], as_json: bool, with_repo: bool) -> None:
    if as_json:
        typer.echo(json.dumps([i.to_json_dict() for i in infos]))
    elif infos:
        typer.echo("\n\n".join(_info_text(i, with_repo) for i in infos))


@pr_app.command("info")
def pr_info(
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON (for status integrations that poll it)"),
    target_branch: str | None = typer.Option(None, "--target-branch", help=_LOOKUP_TARGET_HELP),
    pr_ids: list[str] | None = typer.Option(
        None,
        "--pr-id",
        help="Summarise this PR directly, instead of resolving it from the current git branch (lets you skip having its "
        f"branch checked out locally). Repeat it (or comma-separate) for several, as a list; a PR of another repo is {pr_ref.FORMS}",
    ),
    all_prs: bool = typer.Option(
        False,
        "--all",
        help="Every open PR from the current branch (e.g. one into dev and one into main) instead of exactly one: "
        "with --json a list of the objects, [] and exit 0 when there is none",
    ),
    pat: str | None = typer.Option(None, "--pat", envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"], help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)"),
) -> None:
    """One-shot summary of the PR opened from the current branch, or --pr-id directly: link, state,
    aggregate build state, and the issue(s)/work item(s) it closes. Exits 1 if there is no such PR.
    Build state is judged the way `pr status` does (latest build per pipeline on Azure DevOps).

    --all and several --pr-id values list PRs instead (with --json an array, whatever the count).
    """
    refs: list[pr_ref.PrRef] = []
    values: list[str] = []
    if pr_ids:
        values = pr_ref.split_values(pr_ids)
        if not values:
            raise typer.BadParameter("no PR given", param_hint="--pr-id")
        try:
            parsed = [pr_ref.parse_pr_ref(v, current_remote) for v in values]
        except pr_ref.PrRefError as e:
            raise typer.BadParameter(str(e), param_hint="--pr-id") from None
        here = _current_remote_or_none() if any(r.remote for r in parsed) else None
        # `5,5` and `5,owner/repo#5` are still a list of what they name once
        unique: dict[str, pr_ref.PrRef] = {}
        for ref in (pr_ref.canonical(r, here) for r in parsed):
            unique.setdefault(str(ref).casefold(), ref)
        refs = list(unique.values())
    if all_prs and refs:
        raise typer.BadParameter("--all lists the PRs of the current branch, it can't be combined with --pr-id", param_hint="--all")

    if all_prs:
        _echo_infos(_open_prs_info(target_branch, pat), as_json, with_repo=False)
        return
    if len(values) > 1:
        infos, errors = _ref_infos(refs, pat)
        _echo_infos(infos, as_json, with_repo=True)
        for error in errors:
            typer.echo(f"error: {error}", err=True)
        if errors:
            raise typer.Exit(1)
        return
    if refs and refs[0].remote is not None:
        infos, errors = _ref_infos(refs, pat)
        if errors:
            typer.echo(f"error: {errors[0]}", err=True)
            raise typer.Exit(1)
        info = infos[0]
    else:
        info = _current_pr_info(refs[0].pr_id if refs else None, target_branch, pat)
    if as_json:
        typer.echo(json.dumps(info.to_json_dict()))
        return
    typer.echo(_info_text(info))


@pr_app.command("retry")
def pr_retry(
    target_branch: str | None = typer.Option(None, "--target-branch", help=_LOOKUP_TARGET_HELP),
    pr_id: int | None = typer.Option(None, "--pr-id", help=_PR_ID_HELP),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Retry only the failed job(s)/stage(s) of the most recent build/run for the PR opened
    from the current branch, or for --pr-id directly (Azure DevOps or GitHub, auto-detected),
    instead of a full rerun.
    """
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh = require_gh()
        gh_pr.retry(gh, pr_id=gh_pr.resolve_pr_id(gh, pr_id, target_branch))
        return
    pr_build.retry(remote, pat, target_branch, pr_id=pr_id)


@pr_app.command("watch-deploy")
def pr_watch_deploy(
    target_branch: str = typer.Option(DEFAULT_TARGET_BRANCH, "--target-branch", help="Branch to watch for a directly-triggered build/workflow run (e.g. a post-merge deployment pipeline)"),
    wait: bool = typer.Option(False, "--wait", help="Poll until the build/workflow run(s) are completed; stops early and reports status if one needs manual approval"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Watch the most recent build/workflow run triggered directly on --target-branch -- e.g. a
    pipeline that only runs on the target branch once a PR merges into it and usually does the
    actual deployment -- as opposed to `bdt pr status`, which watches builds/checks tied to a PR
    (Azure DevOps or GitHub, auto-detected).

    No --pr-id here: this watches a branch-triggered run, not any particular PR.
    """
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_pr.run_watch_deploy(require_gh(), target_branch, wait)
        return
    pr_build.run_watch_deploy(remote, pat, target_branch, wait)


@pr_app.command("update")
def pr_update(
    title: str | None = typer.Option(None, "--title", help="New PR title"),
    description: str | None = typer.Option(
        None, "--description", help="New PR description (replaces the existing one)"
    ),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to append to the PR description (repeatable)"
    ),
    file: list[str] = typer.Option(
        [], "--file", help="Path to an arbitrary file to append to the PR description as a linked attachment (repeatable)"
    ),
    target: str | None = typer.Option(None, "--target", help=_LOOKUP_TARGET_HELP),
    pr_id: int | None = typer.Option(None, "--pr-id", help=_PR_ID_HELP),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Update the title/description of the PR opened from the current branch, or --pr-id
    directly (Azure DevOps or GitHub, auto-detected).
    """
    for path in screenshot:
        if not Path(path).is_file():
            raise typer.BadParameter(f"Screenshot not found: {path}", param_hint="--screenshot")
    for path in file:
        if not Path(path).is_file():
            raise typer.BadParameter(f"File not found: {path}", param_hint="--file")
    if title is None and description is None and not screenshot and not file:
        raise typer.BadParameter("Provide at least one of --title, --description, --screenshot, --file")

    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh = require_gh()
        pr_id = gh_pr.resolve_pr_id(gh, pr_id, target)
        # `gh_pr.update()` already re-resolves the branch itself from `--pr-id` (it needs the
        # PR anyway, to fetch its current title/body) -- passing `_gh_branch_for` here would
        # just be a second, redundant `gh pr view` round-trip for the same PR.
        branch = current_branch() if pr_id is None else ""
        gh_pr.update(gh, remote.owner, remote.repo, branch, title, description, screenshot, file, pr_id=pr_id)
    else:
        session, pr = _resolve_ado_pr(pat, remote, target, pr_id=pr_id)
        pr_build.update(session, remote, pr, title, description, screenshot, file)


@pr_app.command("comment")
def pr_comment(
    message: str | None = typer.Option(None, "--message", help="Comment text"),
    message_file: str | None = typer.Option(
        None, "--message-file", help="Read the comment text from this file (`-` = stdin), e.g. a markdown plan"
    ),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to embed in the comment (repeatable)"
    ),
    file: list[str] = typer.Option(
        [], "--file", help="Path to an arbitrary file to link in the comment as an attachment (repeatable)"
    ),
    target: str | None = typer.Option(None, "--target", help=_LOOKUP_TARGET_HELP),
    pr_id: int | None = typer.Option(None, "--pr-id", help=_PR_ID_HELP),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Post a comment on the PR opened from the current branch, or --pr-id directly
    (Azure DevOps or GitHub, auto-detected).
    """
    if message_file is not None:
        if message is not None:
            raise typer.BadParameter("pass either --message or --message-file, not both", param_hint="--message-file")
        try:
            message = sys.stdin.read() if message_file == "-" else Path(message_file).read_text(encoding="utf-8")
        except OSError as e:
            raise typer.BadParameter(f"cannot read {message_file!r}: {e}", param_hint="--message-file") from e
    for path in screenshot:
        if not Path(path).is_file():
            raise typer.BadParameter(f"Screenshot not found: {path}", param_hint="--screenshot")
    for path in file:
        if not Path(path).is_file():
            raise typer.BadParameter(f"File not found: {path}", param_hint="--file")
    if not message and not screenshot and not file:
        raise typer.BadParameter("Provide at least one of --message, --message-file, --screenshot, --file")

    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh = require_gh()
        pr_id = gh_pr.resolve_pr_id(gh, pr_id, target)
        branch = _gh_branch_for(gh, pr_id, needed=bool(screenshot or file))
        gh_pr.comment_with_screenshots(gh, remote.owner, remote.repo, branch, message, screenshot, file, pr_id=pr_id)
    else:
        session, pr = _resolve_ado_pr(pat, remote, target, pr_id=pr_id)
        pr_build.comment_with_screenshots(session, remote, pr["pullRequestId"], message, screenshot, file)


@issue_app.command("create")
def issue_create(
    title: str = typer.Option(..., "--title", help="Issue / work item title"),
    description: str | None = typer.Option(None, "--description", help="Issue / work item description body"),
    type_: str = typer.Option("Bug", "--type", help="Work item type, e.g. Bug, Task, User Story (Azure DevOps only)"),
    board: str | None = typer.Option(
        None,
        "--board",
        help="Board to file the issue/work item against, so it shows up there: an Azure Boards team "
        r"(sets its Area Path; overrides \[tool.bdt.ado].board in pyproject.toml) or a GitHub Projects "
        r"(v2) board by title (overrides \[tool.bdt.github].board)",
    ),
    label: list[str] = typer.Option(
        [], "--label", help="Label/tag to apply (repeatable). Alias for --tag -- routes to labels on GitHub, tags on Azure DevOps."
    ),
    tag: list[str] = typer.Option(
        [], "--tag", help="Label/tag to apply (repeatable). Alias for --label -- routes to tags on Azure DevOps, labels on GitHub."
    ),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to attach to the issue / work item (repeatable)"
    ),
    file: list[str] = typer.Option(
        [], "--file", help="Path to an arbitrary file to attach to the issue / work item as a linked attachment (repeatable)"
    ),
    iteration: str | None = typer.Option(
        None,
        "--iteration",
        help=r"Iteration (sprint) Path to set, e.g. 'MyProject\Sprint 1' (Azure DevOps only; overrides \[tool.bdt.ado].iteration "
        "in pyproject.toml). Work items without one don't show on the team backlog in Basic-process projects.",
    ),
    parent: int | None = typer.Option(
        None, "--parent", help="Make the new issue / work item a child (sub-issue) of this issue / work item ID"
    ),
    blocked_by: list[int] = typer.Option(
        [], "--blocked-by", help="Issue / work item ID blocking the new one -- its predecessor on Azure DevOps (repeatable)"
    ),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
    args: list[str] = typer.Argument(None, help="Extra args (GitHub only): --assignee, --milestone"),
) -> None:
    """Create a new issue / work item (Azure DevOps or GitHub, auto-detected)."""
    for path in screenshot:
        if not Path(path).is_file():
            raise typer.BadParameter(f"Screenshot not found: {path}", param_hint="--screenshot")
    for path in file:
        if not Path(path).is_file():
            raise typer.BadParameter(f"File not found: {path}", param_hint="--file")

    tags_or_labels = _merge_tags_and_labels(tag, label) or []
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        _reject_ado_only_options(iteration=iteration)
        resolved_board = gh_issue.resolve_board(board)
        gh_issue.create(
            require_gh(), remote.owner, remote.repo, title, description, tags_or_labels, screenshot, args or [], file_paths=file, board=resolved_board,
            parent=parent, blocked_by=blocked_by,
        )
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        resolved_board = ado_issue.check_board(ado_issue.resolve_board(board), creating=True)
        ado_issue.create(
            session, remote, type_, title, description, resolved_board, tags_or_labels, screenshot, file,
            iteration=ado_issue.resolve_iteration(iteration), parent=parent, blocked_by=blocked_by,
        )


@issue_app.command("search")
def issue_search(
    keywords: list[str] = typer.Argument(
        None, help="Keywords to search for (ANDed together); omit to just list issues/work items"
    ),
    since_days: int = typer.Option(
        30, "--since-days", help="Only include issues/work items updated within this many days (0 = no date filter)"
    ),
    state: str = typer.Option("open", "--state", help="Filter by state: 'open', 'closed', or 'all'"),
    board: str | None = typer.Option(
        None,
        "--board",
        help="Scope the search to this board: an Azure Boards team's Area Path subtree "
        r"(falls back to \[tool.bdt.ado].board) or a GitHub Projects (v2) board by title or number "
        r"(falls back to \[tool.bdt.github].board), same as `issue create`",
    ),
    limit: int = typer.Option(10, "--limit", help="Max results to return"),
    org_wide: bool = typer.Option(
        False,
        "--org-wide",
        help="Search every project in the Azure DevOps org, or every repo owned by the GitHub owner, "
        "instead of just the current one. Can't be combined with --board. Outside of any git repo (or "
        "in one with no recognized origin remote), falls back to --org/--github-org instead of failing.",
    ),
    org: str | None = typer.Option(
        None,
        "--org",
        envvar=["AZDO_ORG", "BMS_ORG"],
        help="Azure DevOps org to --org-wide search when there's no git repo (or no recognized remote) "
        "to detect one from. Ignored whenever the current repo's remote resolves fine -- so it's safe "
        "to leave set in your shell profile (e.g. for `find-repo`) without affecting a normal, in-repo "
        "`issue search`.",
    ),
    github_org: str | None = typer.Option(
        None,
        "--github-org",
        envvar=["GITHUB_ORG", "BMS_GITHUB_ORG"],
        help="GitHub owner/org to --org-wide search when there's no git repo (or no recognized remote) "
        "to detect one from. Ignored whenever the current repo's remote resolves fine -- so it's safe "
        "to leave set in your shell profile (e.g. for `find-repo`) without affecting a normal, in-repo "
        "`issue search`.",
    ),
    label: list[str] = typer.Option(
        [], "--label", help="Only issues/work items carrying this label/tag, ANDed (repeatable). Alias for --tag."
    ),
    tag: list[str] = typer.Option(
        [], "--tag", help="Only issues/work items carrying this label/tag, ANDed (repeatable). Alias for --label."
    ),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
    business_user: bool = typer.Option(
        False,
        "--business-user",
        help="Azure DevOps only: only work items created by a business user -- someone with a Stakeholder license, or "
        "(when the license can't be read, or isn't Stakeholder) with no commit in the last --commit-depth commits of "
        "this repo. The license lookup needs a PAT with the 'Member Entitlement Management (read)' scope.",
    ),
    commit_depth: int = typer.Option(
        business_user_mod.DEFAULT_COMMIT_DEPTH,
        "--commit-depth",
        min=1,
        help="With --business-user: how many of the latest commits of this repo count when checking whether a creator ever committed",
    ),
    for_me: bool = typer.Option(
        False,
        "--for-me",
        help="Only issues/work items whose LAST comment addresses the calling coding agent: contains "
        "@Claude / @Codex / @Goose / ... (case-insensitive, whole word; which one is detected from the "
        "environment, see --agent) or @Agent (addresses every agent).",
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        envvar=agent_mentions.AGENT_ENV_VAR,
        help="With --for-me: the agent to look for mentions of, e.g. claude, codex, goose. Default: detected "
        "from the environment (CLAUDECODE, AI_AGENT, CODEX_*, GOOSE_*, ...); without any, only @Agent matches.",
    ),
    take: bool = typer.Option(
        False,
        "--take",
        help="With --for-me: return only the first matching issue/work item and claim it in the same go, "
        "like `issue take` does (--limit then only sizes how many candidates are scanned). With --org-wide the claim is "
        "posted on the match's own repo / project.",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="With --for-me: instead of reporting that nothing matches, keep polling until an issue/work item does, then "
        "behave as if the search had found it right away (with --take: claim it). Exits 4 if none turned up "
        "within --timeout (a progress note goes to stderr, stdout stays what a plain search prints).",
    ),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        help=f"With --wait: give up after this many seconds (0 = wait forever; default {issue_watch_mod.DEFAULT_TIMEOUT:g}); exits 4 on timeout",
    ),
    interval: float | None = typer.Option(
        None, "--interval", help=f"With --wait: seconds between polls (default {issue_watch_mod.DEFAULT_INTERVAL:g})"
    ),
) -> None:
    """Search (or, with no keywords, just list) issues / work items, defaulting to open issues
    from the last 30 days (Azure DevOps or GitHub, auto-detected).
    """
    if state not in ("open", "closed", "all"):
        raise typer.BadParameter("Must be one of: open, closed, all", param_hint="--state")
    if business_user and org_wide:
        raise typer.BadParameter("--business-user checks the commits of this repo, so it can't be combined with --org-wide", param_hint="--business-user")
    if commit_depth != business_user_mod.DEFAULT_COMMIT_DEPTH and not business_user:
        raise typer.BadParameter("--commit-depth only works together with --business-user", param_hint="--commit-depth")
    if take and not for_me:
        raise typer.BadParameter("--take only works together with --for-me", param_hint="--take")
    if take and limit < 1:
        raise typer.BadParameter("--take needs --limit of at least 1 (it sizes the candidates scanned)", param_hint="--limit")
    if wait and not for_me:
        raise typer.BadParameter("--wait only works together with --for-me (without it, a search just lists)", param_hint="--wait")
    if not wait and (timeout is not None or interval is not None):
        raise typer.BadParameter("--timeout and --interval only work together with --wait", param_hint="--timeout" if timeout is not None else "--interval")
    _check_poll_options(interval, timeout)
    # only passed on with --for-me, so a plain search calls the backends exactly as before.
    # --take wants one match out of the same scan a plain --for-me does: --limit then sizes the scan, not the result
    resolved_agent = _resolve_agent(agent) if for_me else None
    search_extra: dict = {"comment_filter": agent_mentions.last_comment_filter(resolved_agent)} if for_me else {}
    if take:
        search_extra["scan_limit"] = limit
        limit = 1
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d") if since_days > 0 else None
    tags_or_labels = _merge_tags_and_labels(tag, label) or []

    # --org/--github-org are only ever consulted as a *fallback*, when `current_remote()` itself
    # fails (no git repo, no origin remote, or a remote that's neither GitHub nor Azure DevOps) --
    # never merely because one happens to be set. Both envvars are shared with the pre-existing
    # `find-repo` command, which documents setting them persistently in a shell profile (and
    # having *both* set, to search either host); gating on "is --org/--github-org set" instead of
    # "did detecting the repo's own remote fail" would make an ordinary, in-repo `bdt issue
    # search` break for anyone who followed that advice. `--org-wide` without either flag/envvar
    # keeps today's behavior exactly: `current_remote()`'s own error propagates unchanged.
    if org_wide:
        try:
            remote: AdoRemote | GitHubRemote = current_remote()
        except (SystemExit, UnknownRemoteError):
            if org and github_org:
                raise typer.BadParameter("Pass only one of --org or --github-org", param_hint="--org") from None
            if github_org:
                remote = GitHubRemote(github_org, "")
            elif org:
                remote = AdoRemote(org, "", "")
            else:
                raise
    else:
        remote = current_remote()

    if business_user and isinstance(remote, GitHubRemote):
        raise typer.BadParameter("--business-user is only available for Azure DevOps work items", param_hint="--business-user")
    # one detector for all polls of --wait: it caches the git history and the licenses it asked for
    detector: business_user_mod.BusinessUserDetector | None = None

    def search_once() -> tuple[list[dict], tuple[AdoRemote | GitHubRemote, int] | None]:
        """One search (the backend prints the matches, or "No matching ...", itself); with the first match's
        remote and number, which is where `--take` claims it."""
        nonlocal detector
        if isinstance(remote, GitHubRemote):
            resolved_board = gh_issue.resolve_board(board)
            items = gh_issue.search(
                require_gh(), remote.owner, remote.repo, keywords or [], since, limit, state, board=resolved_board, org_wide=org_wide, labels=tags_or_labels, **search_extra
            )
            found = (_github_item_remote(items[0]) if org_wide else remote, items[0]["number"]) if items else None
        else:
            session = requests.Session()
            session.headers.update(auth_header(pat))
            resolved_board = ado_issue.resolve_board(board)
            if business_user:
                detector = detector or business_user_mod.BusinessUserDetector(session, remote, commit_depth)
                search_extra["business_users"] = detector
            items = ado_issue.search(
                session, remote, keywords or [], since=since, board=resolved_board, top=limit, state=state, org_wide=org_wide, tags=tags_or_labels, **search_extra
            )
            project = items[0]["fields"].get("System.TeamProject") if items and org_wide else None
            found = (replace(remote, project=project) if project else remote, items[0]["id"]) if items else None
        return items, found

    if wait:
        found = _wait_for_match(
            search_once,
            agent=resolved_agent,
            interval=issue_watch_mod.DEFAULT_INTERVAL if interval is None else interval,
            timeout=issue_watch_mod.DEFAULT_TIMEOUT if timeout is None else timeout,
        )
    else:
        found = search_once()[1]

    if take and found is not None:
        # an --org-wide match can live in any repo / project of the org: claim it where it is, not in the current one
        target, number = found
        user = _current_user(target)
        if not _take(target, number, f"{issue_do_mod.TAKEN_PREFIX} {user}", pat, user=user):
            raise typer.Exit(1)
        if org_wide and target != remote:
            typer.echo(_find_repo_hint(target))


def _wait_for_match[T](
    search_once: Callable[[], tuple[list[dict], T]], *, agent: str | None, interval: float, timeout: float
) -> T:
    """`issue search --wait`: repeat `search_once` until it finds something, then print what it printed and return its
    second result. The backends print as they search ("No matching ..." included), so each poll runs with stdout captured
    and only the poll that matched is replayed -- stdout ends up exactly what a plain search that found it prints. Exits
    `EXIT_TIMEOUT` (4, like `issue watch`) when nothing matched within `timeout` seconds (0 = never give up)."""
    typer.echo(
        f"Waiting {f'up to {timeout:g} s' if timeout else 'with no timeout'} for an issue / work item that addresses "
        f"@{agent or 'Agent'} (polling every {interval:g} s) ...",
        err=True,
    )

    def probe() -> tuple[str, T] | None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            items, found = search_once()
        return (out.getvalue(), found) if items else None

    polled = issue_watch_mod.poll_until(probe, interval=interval, timeout=timeout or None)
    if polled is None:
        typer.echo(f"No issue / work item addressing @{agent or 'Agent'} turned up within {timeout:g} s", err=True)
        raise typer.Exit(issue_watch_mod.EXIT_TIMEOUT)
    typer.echo(polled[0], nl=False)
    return polled[1]


def _origin_is(clone: Path, target: GitHubRemote) -> bool:
    """Is `clone`'s `origin` the GitHub repo `target`? (A same-named repo of another owner is not.)"""
    try:
        origin = parse_remote(origin_url(str(clone)))
    except (SystemExit, UnknownRemoteError, NotGitHubRemoteError):
        return False
    return isinstance(origin, GitHubRemote) and (origin.owner.lower(), origin.repo.lower()) == (target.owner.lower(), target.repo.lower())


def _find_repo_hint(target: AdoRemote | GitHubRemote) -> str:
    """Where a claimed `--org-wide` match lives and where its repo is: the local clone when there is one, else how to get
    it (`bdt find-repo`) -- it is usually not the checkout `bdt` ran in. An Azure DevOps work item belongs to a project,
    not a repo, so there is no clone to name."""
    if isinstance(target, GitHubRemote):
        root = find_repo_mod.work_dir()
        candidates = find_repo_mod.find_local(target.repo, root) if root.is_dir() else []
        clones = [p for p in candidates if p.name.lower() == target.repo.lower() and _origin_is(p, target)]
        if clones:
            return f"This issue is in {target.owner}/{target.repo}, not the current repo -- local clone: {', '.join(str(p) for p in clones)}"
        return f"This issue is in {target.owner}/{target.repo}, not the current repo, and not cloned yet -- find (and clone) it with `bdt find-repo {target.repo}`."
    return (
        f"This work item is in the Azure DevOps project {target.project}, not necessarily the current repo -- "
        "find (and clone) the repo it concerns with `bdt find-repo <repo name>`."
    )


def _check_poll_options(interval: float | None, timeout: float | None) -> None:
    """`--interval` / `--timeout` of the polling commands: finite numbers (nan/inf would poll forever or crash `sleep`);
    a timeout of 0 means wait forever."""
    if interval is not None and not (math.isfinite(interval) and interval > 0):
        raise typer.BadParameter("Must be a number greater than 0", param_hint="--interval")
    if timeout is not None and not (math.isfinite(timeout) and timeout >= 0):
        raise typer.BadParameter("Must be a number, 0 (wait forever) or greater", param_hint="--timeout")


def _github_item_remote(item: dict) -> GitHubRemote:
    """The repo an `--org-wide` GitHub search result (`repository.nameWithOwner`) lives in."""
    owner, _, repo = item["repository"]["nameWithOwner"].partition("/")
    return GitHubRemote(owner, repo)


def _resolve_agent(agent: str | None) -> str | None:
    """The agent to match `@mentions` of: `--agent`/`BDT_AGENT`, else detected from the environment.
    None (with a note on stderr) when neither -- then only `@Agent` mentions can match."""
    resolved = agent_mentions.detect_agent(override=agent)
    if resolved is None:
        typer.echo("No calling agent detected (pass --agent or set BDT_AGENT) -- only @Agent mentions match", err=True)
    return resolved


@issue_app.command("do", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def issue_do(
    ctx: typer.Context,
    number: int = typer.Argument(..., help="Issue number (GitHub) or work item ID (Azure DevOps)"),
    agent: str = typer.Option("claude", "--agent", help="Agent executable to start (claude runs headless: `claude -p ... --name '<number>: <title>'`)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the agent command instead of running it"),
    force: bool = typer.Option(False, "--force", help="Start even if someone else already holds the claim (comments 'Taken by' anyway)"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Start a coding agent on an issue / work item, with the session named '<number>: <title>'.
    Extra args after the number are passed through to the agent."""
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        title, body = issue_do_mod.fetch_github(require_gh(), number)
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        title, body = issue_do_mod.fetch_ado(session, remote, number)
    extra = list(ctx.args)
    # claude can be given the session id up front, so the "Taken by" comment can link the session that will exist
    session_id = str(uuid.uuid4()) if agent == "claude" and "--session-id" not in extra else None
    if dry_run:
        typer.echo(f"(dry run: would comment 'Taken by ...' on #{number} first)", err=True)
    else:
        user = _current_user(remote)
        if not _take(remote, number, issue_do_mod.take_message(user, agent, session_id), pat, user=user, force=force, agent_note=False):
            raise typer.Exit(1)
    issue_do_mod.run(number, title, body, agent=agent, extra=extra, dry_run=dry_run, session_id=session_id)


@issue_app.command("take")
def issue_take(
    number: int | None = typer.Argument(None, help="Issue number (GitHub) or work item ID (Azure DevOps). Default: the issue the current branch's PR closes"),
    target_branch: str | None = typer.Option(None, "--target-branch", help=_LOOKUP_TARGET_HELP + " (only used to find the issue when no number is given)"),
    force: bool = typer.Option(False, "--force", help="Comment even if someone else already holds the claim"),
    pat: str | None = typer.Option(None, "--pat", envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"], help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)"),
) -> None:
    """Claim an issue / work item: comment "Taken by <you>" on it, with this coding agent's
    session link (detected automatically) so others can see who is working on it and find the session.
    """
    remote = current_remote()
    if number is None:
        issues = _current_pr_info(None, target_branch, pat).issues
        if len(issues) != 1:
            raise typer.BadParameter(
                f"The current branch's PR closes {len(issues)} issues, so can't tell which to take -- pass the number"
                if issues
                else "No issue number given, and the current branch's PR doesn't reference one (e.g. 'Fixes #N')"
            )
        number = issues[0].number
    user = _current_user(remote)
    if not _take(remote, number, f"{issue_do_mod.TAKEN_PREFIX} {user}", pat, user=user, force=force):
        raise typer.Exit(1)


def _take(
    remote: AdoRemote | GitHubRemote,
    number: int,
    message: str,
    pat: str | None,
    *,
    user: str,
    force: bool = False,
    agent_note: bool = True,
) -> bool:
    """Comment `message` ("Taken by ...") on the issue -- unless its newest comment already is such a
    claim, so running this twice (or `issue do` after `issue take`) doesn't post it again. If that
    claim is someone else's, nothing is posted without `force`. True if `user` holds the claim now."""
    kind = "issue" if isinstance(remote, GitHubRemote) else "work item"
    last = _last_comment(remote, number, pat)
    if issue_do_mod.is_take_comment(last):
        claimant = issue_do_mod.claimant(last)
        if claimant.casefold() == user.casefold():
            typer.echo(f"{kind.capitalize()} #{number} is already taken by you (its last comment says so) -- not commenting again")
            return True
        if not force:
            typer.echo(f"{kind.capitalize()} #{number} is already taken by {claimant} -- pass --force to take it anyway", err=True)
            return False
        typer.echo(f"{kind.capitalize()} #{number} was taken by {claimant} -- taking it anyway (--force)", err=True)
    typer.echo(f"Taking {kind} #{number}")
    _comment_on_issue(remote, number, message, pat, agent_note=agent_note)
    return True


def _last_comment(remote: AdoRemote | GitHubRemote, number: int, pat: str | None) -> str | None:
    if isinstance(remote, GitHubRemote):
        return gh_issue.last_comment(require_gh(), remote.owner, remote.repo, number)
    session = requests.Session()
    session.headers.update(auth_header(pat))
    return ado_issue.last_comment(session, remote, number)


def _comment_on_issue(remote: AdoRemote | GitHubRemote, number: int, message: str, pat: str | None, *, agent_note: bool = True) -> None:
    if isinstance(remote, GitHubRemote):
        gh_issue.comment(require_gh(), remote.owner, remote.repo, number, message, [], agent_note=agent_note)
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        ado_issue.comment_with_screenshots(session, remote, number, message, [], [], agent_note=agent_note)


def _current_user(remote: AdoRemote | GitHubRemote) -> str:
    """Who to say took an issue: the GitHub login the token belongs to, else the git `user.name`."""
    if isinstance(remote, GitHubRemote):
        try:
            login = require_gh().get("user").get("login")
        except (SystemExit, ValueError, AttributeError):
            login = None
        if login:
            return login
    r = _capture(["git", "config", "user.name"])
    # `git config user.name` comes from the (possibly cloned-from-elsewhere) repo's own config and ends up in a
    # markdown/HTML comment, so keep it to plain name characters: no links, tags or @mentions
    name = re.sub(r"[^\w .'-]", "", r.stdout if r else "").strip()[:64]
    return name or "unknown"


def _capture(cmd: list[str]) -> subprocess.CompletedProcess | None:
    """Run `cmd` for its UTF-8 output, bounded so a stalled call can't hang the
    caller; None on timeout."""
    try:
        return subprocess.run(cmd, capture_output=True, encoding="utf-8", timeout=CLI_TIMEOUT_SECS, check=False)
    except subprocess.TimeoutExpired:
        return None


def issue_watch(
    number: int = typer.Argument(..., help="Issue number (GitHub) or work item ID (Azure DevOps)"),
    interval: float = typer.Option(issue_watch_mod.DEFAULT_INTERVAL, "--interval", help="Seconds between polls"),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        help=f"Give up after this many seconds (0 = wait forever; default {issue_watch_mod.DEFAULT_TIMEOUT:g}, or 0 with --follow); exits 4 on timeout, "
        "except with --follow, where reaching it ends the stream normally",
    ),
    follow: bool = typer.Option(
        False,
        "--follow",
        help="Keep running and print every new comment as it arrives (always one JSON line each, see --json) instead of exiting "
        "after the first batch -- for a background process whose output lines are notifications (e.g. Claude Code's Monitor tool). "
        "Tolerates a longer outage of the API than a plain watch; if it still exits non-zero, restart it with --since-comment <last id>",
    ),
    since_comment: int | None = typer.Option(
        None,
        "--since-comment",
        help="Report comments newer than this comment ID (as printed by `issue comment add`), even ones already "
        "posted before the watch started. Default: only comments arriving after the watch started.",
    ),
    for_me: bool = typer.Option(
        False, "--for-me", help="Only report comments that mention the calling agent (@Claude / @Codex / ...) or @Agent -- see `issue search --for-me`"
    ),
    agent: str | None = typer.Option(
        None, "--agent", envvar=agent_mentions.AGENT_ENV_VAR, help="With --for-me: the agent to look for mentions of (default: detected from the environment)"
    ),
    skip_own: bool = typer.Option(
        False,
        "--skip-own",
        help="Ignore comments this agent wrote itself: those carrying its session note (appended by bdt to every comment "
        "it posts), or, outside an agent, those authored by the current user",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print each new comment as one JSON line (id, author, created_at, url, body)"),
    pat: str | None = typer.Option(None, "--pat", envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"], help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)"),
) -> None:
    """Wait for new comments on an issue / work item -- so an agent can ask a question in the issue and
    block until someone answers. Prints the new comment(s) and exits 0; exits 4 if none arrived within
    --timeout. With --follow it keeps printing every new comment instead. Progress notes go to stderr, so
    stdout holds only the comments (JSON lines with --json).
    """
    if timeout is None:
        timeout = 0 if follow else issue_watch_mod.DEFAULT_TIMEOUT
    _check_poll_options(interval, timeout)
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh = require_gh()

        def fetch() -> list[issue_watch_mod.Comment]:
            return issue_watch_mod.to_comments(gh_issue.list_comments(gh, remote.owner, remote.repo, number))
    else:
        ado_session = requests.Session()
        ado_session.headers.update(auth_header(pat))

        def fetch() -> list[issue_watch_mod.Comment]:
            return issue_watch_mod.to_comments(ado_issue.list_comments(ado_session, remote, number), is_html=True)

    own_session = detect_agent_session() if skip_own else None
    accept = issue_watch_mod.build_accept(
        agent=_resolve_agent(agent) if for_me else None,
        for_me=for_me,
        own_session_id=own_session.session_id if own_session else None,
        own_author=_current_user(remote) if skip_own and own_session is None else None,
    )
    typer.echo(f"Watching #{number} for new comments (every {interval:g}s, {f'up to {timeout:g}s' if timeout else 'no timeout'})", err=True)
    if follow:
        as_json = True  # one line per comment: each stdout line of a background process is one notification
    batches = issue_watch_mod.follow(
        fetch,
        since_id=since_comment,
        accept=accept,
        interval=interval,
        timeout=timeout or None,
        max_errors=issue_watch_mod.FOLLOW_MAX_CONSECUTIVE_ERRORS if follow else issue_watch_mod.MAX_CONSECUTIVE_ERRORS,
    )
    reported = False
    for batch in batches:
        reported = True
        for comment in batch:
            typer.echo(issue_watch_mod.format_comment(comment, as_json=as_json))
            if not as_json:
                typer.echo("")
        if not follow:
            return
    if not reported and not follow:
        typer.echo(f"No new comment on #{number} within {timeout:g}s", err=True)
        raise typer.Exit(issue_watch_mod.EXIT_TIMEOUT)


issue_app.command("watch")(issue_watch)
app.command("watch-issue", help="Alias for `bdt issue watch`")(issue_watch)


@issue_app.command("show")
def issue_show(
    number: int = typer.Argument(..., help="Issue number (GitHub) or work item ID (Azure DevOps)"),
    as_json: bool = typer.Option(False, "--json", help="Print one JSON document instead of text"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Show an issue / work item (Azure DevOps or GitHub, auto-detected): state, title, description, tags,
    area / iteration (Azure DevOps), links to other issues / work items (parent, children, blocked by, blocks) and comments.
    """
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        details = gh_issue.get_details(require_gh(), remote.owner, remote.repo, number)
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        details = ado_issue.get_details(session, remote, number, business_user_mod.BusinessUserDetector(session, remote))
    typer.echo(issue_show_mod.format_details(details, as_json=as_json))


@issue_app.command("update")
def issue_update(
    number: int = typer.Argument(..., help="Issue number (GitHub) or work item ID (Azure DevOps)"),
    title: str | None = typer.Option(None, "--title", help="New title"),
    description: str | None = typer.Option(None, "--description", help="New description body (replaces the existing one)"),
    board: str | None = typer.Option(
        None,
        "--board",
        help="Move the work item to this Azure Boards team's Area Path (Azure DevOps only). Unlike `issue create`, "
        r"this is only applied when explicitly given here — it does not fall back to \[tool.bdt.ado].board, "
        "so an unrelated field update can't silently move the item onto a different board.",
    ),
    state: str | None = typer.Option(
        None,
        "--state",
        help="New state. Both backends understand 'Open', 'Closed'/'Done' (closes as completed), and "
        "'Removed'/'Not Planned' (closes as not planned); other values (e.g. Active, Resolved) only "
        "apply where that exact state name exists for the work item's type — elsewhere the state is "
        "left unchanged and a comment records what was requested.",
    ),
    tag: list[str] | None = typer.Option(
        None,
        "--tag",
        help="Tag/label to add (repeatable); omit to leave unchanged. Alias for --label -- routes to labels on "
        "GitHub (added alongside existing labels) or tags on Azure DevOps (replaces the full tag list, "
        "combined with --remove-tag/--remove-label if also given).",
    ),
    label: list[str] | None = typer.Option(
        None, "--label", help="Tag/label to add (repeatable); omit to leave unchanged. Alias for --tag."
    ),
    remove_tag: list[str] | None = typer.Option(
        None,
        "--remove-tag",
        help="Tag/label to remove (repeatable). Alias for --remove-label -- on Azure DevOps this removes just "
        "the named tag(s), leaving the rest untouched (unlike --tag/--label, which replace the whole set).",
    ),
    remove_label: list[str] | None = typer.Option(
        None, "--remove-label", help="Tag/label to remove (repeatable). Alias for --remove-tag."
    ),
    iteration: str | None = typer.Option(
        None,
        "--iteration",
        help=r"Move the work item to this Iteration (sprint) Path, e.g. 'MyProject\Sprint 1' (Azure DevOps only). Like --board, "
        r"only applied when given here -- it does not fall back to \[tool.bdt.ado].iteration.",
    ),
    parent: int | None = typer.Option(
        None,
        "--parent",
        help="Make this issue / work item a child (sub-issue) of this issue / work item ID; on Azure DevOps, replaces its current parent",
    ),
    blocked_by: list[int] = typer.Option(
        [], "--blocked-by", help="Issue / work item ID blocking this one -- its predecessor on Azure DevOps (repeatable). Already-set links are left alone."
    ),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Update an issue / work item's fields (Azure DevOps or GitHub, auto-detected)."""
    tags_or_labels = _merge_tags_and_labels(tag, label)
    removals = _merge_tags_and_labels(remove_tag, remove_label)
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        _reject_ado_only_options(iteration=iteration)
        gh_issue.update(require_gh(), number, title, description, tags_or_labels, removals, state, parent, blocked_by)
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        ado_issue.update(
            session, remote, number, title, description, ado_issue.check_board(board), tags_or_labels, removals, state,
            iteration=iteration, parent=parent, blocked_by=blocked_by,
        )


@issue_app.command("delete")
def issue_delete(
    number: int = typer.Argument(..., help="Issue number (GitHub) or work item ID (Azure DevOps)"),
    yes: bool = typer.Option(False, "--yes", help="Confirm deletion without prompting"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Delete an issue / work item (Azure DevOps or GitHub, auto-detected).

    Azure DevOps soft-deletes to the project's Recycle Bin (restorable).
    GitHub deletion is permanent — there's no recycle bin for issues.
    """
    if not yes:
        raise typer.BadParameter("Pass --yes to confirm deletion.", param_hint="--yes")

    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_issue.delete(require_gh(), number)
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        ado_issue.delete(session, remote, number)


@issue_comment_app.command("add")
def issue_comment_add(
    number: int = typer.Argument(..., help="Issue number (GitHub) or work item ID (Azure DevOps)"),
    message: str | None = typer.Option(None, "--message", help="Comment text"),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to embed in the comment (repeatable)"
    ),
    file: list[str] = typer.Option(
        [], "--file", help="Path to an arbitrary file to link in the comment as an attachment (repeatable)"
    ),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Post a comment on an issue / work item (Azure DevOps or GitHub, auto-detected)."""
    for path in screenshot:
        if not Path(path).is_file():
            raise typer.BadParameter(f"Screenshot not found: {path}", param_hint="--screenshot")
    for path in file:
        if not Path(path).is_file():
            raise typer.BadParameter(f"File not found: {path}", param_hint="--file")
    if not message and not screenshot and not file:
        raise typer.BadParameter("Provide at least one of --message, --screenshot, --file")

    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_issue.comment(require_gh(), remote.owner, remote.repo, number, message, screenshot, file)
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        ado_issue.comment_with_screenshots(session, remote, number, message, screenshot, file)


@issue_comment_app.command("update")
def issue_comment_update(
    number: int = typer.Argument(..., help="Issue number (GitHub) or work item ID (Azure DevOps)"),
    comment_id: int = typer.Argument(..., help="Comment ID, as printed by `issue comment add`"),
    message: str = typer.Option(..., "--message", help="New comment text"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Edit an existing comment on an issue / work item (Azure DevOps or GitHub, auto-detected)."""
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_issue.update_comment(require_gh(), remote.owner, remote.repo, str(comment_id), message)
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        ado_issue.update_comment(session, remote, number, comment_id, message)


@issue_comment_app.command("delete")
def issue_comment_delete(
    number: int = typer.Argument(..., help="Issue number (GitHub) or work item ID (Azure DevOps)"),
    comment_id: int = typer.Argument(..., help="Comment ID, as printed by `issue comment add`"),
    yes: bool = typer.Option(False, "--yes", help="Confirm deletion without prompting"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Delete a comment on an issue / work item (Azure DevOps or GitHub, auto-detected)."""
    if not yes:
        raise typer.BadParameter("Pass --yes to confirm deletion.", param_hint="--yes")

    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_issue.delete_comment(require_gh(), remote.owner, remote.repo, str(comment_id))
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        ado_issue.delete_comment(session, remote, number, comment_id)


@app.command()
def worktree(
    name: str,
    base: str = typer.Option("dev", "--base", help="Branch to base the new worktree on"),
    env_file: str | None = typer.Option(None, "--env-file", help="File to copy into the worktree as .env (default: auto-detect .local_env then .env)"),
    submodules: bool = typer.Option(True, "--submodules/--no-submodules", help="Run `git submodule update --init` in the new worktree"),
    install: str | None = typer.Option(None, "--install", help="Shell command to run inside the new worktree after creation, e.g. 'just install'"),
) -> None:
    """Create a git worktree under .worktrees/<name>, mirroring the `just worktree` recipe."""
    install_cmd = install.split() if install else None
    worktree_mod.create(name, base=base, env_file=env_file, submodules=submodules, install_cmd=install_cmd)


@app.command()
def pull(
    remote: str = typer.Option(
        "origin",
        "--remote",
        help="Remote to pull main/master and the default branch from (the current branch's own tracking "
        "branch step always follows its real configured upstream, regardless of this)",
    ),
    no_default: bool = typer.Option(
        False,
        "--no-default",
        help="Don't also pull the repo's DEFAULT branch -- skip this when the current branch was already "
        "branched from main/master, since pulling it again would be redundant",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be pulled, without doing it"),
    pull_args: list[str] = typer.Argument(
        None,
        help="Extra flags passed through to every `git pull` step, e.g. `-- --rebase --ff-only` (put them after `--`)",
    ),
) -> None:
    """Bring the current branch up to date from three sources: its own remote tracking branch, the
    remote's main/master branch, and the repo's DEFAULT branch (unless --no-default) -- each a
    separate `git pull`, stopping with a clear error (and the exact command to re-run) if any step
    hits a merge conflict.
    """
    pull_mod.run(remote=remote, no_default=no_default, dry_run=dry_run, pull_args=pull_args or [])


@app.command("lint")
def lint(
    paths: list[str] = typer.Argument(
        None,
        help="Files and/or directories to scan (default: current directory, recursive). Pass an explicit "
        "list of files -- e.g. from a prek/pre-commit hook's staged-file list -- to lint only those.",
    ),
    no_tooling_check: bool = typer.Option(
        False,
        "--no-tooling-check",
        help="Skip the ty/ruff/pytest/prek tooling-config check. Also bypassable per-repo via "
        "[tool.bdt.lint] skip_tooling_check = true in pyproject.toml.",
    ),
) -> None:
    """Static checks (bmsuisse/skills#52): postgres/psycopg SQL rules on every `.execute()` call and pgdevkit
    `fetch_all`/`fetch_one`/`fetch_scalar`/`execute`/`PostgresJsonResponse` call (and arrowbricks'
    `execute_streamed`/`query_agent`/`stream_query_json`, and the project's own `[tool.bdt.lint] sql_functions`; the same
    rules for every dialect, Databricks SQL included)
    (must use load_sql()/a .sql file, a t-string, or psycopg.sql for anything beyond a trivial
    query; never an f-string/concatenation/`%`-formatting), pydantic-model placement under api/
    directories, hand-wired HTTP in TypeScript, and that the repo declares/configures ty, ruff, pytest and prek.
    For unused .sql files and dead backend routes see `dead-code`.
    """
    result = lint_mod.run(paths or [], skip_tooling_check=no_tooling_check)
    raise typer.Exit(lint_mod.print_report(result))


@app.command("dead-code")
def dead_code(
    only: list[str] = typer.Option(
        None,
        "--only",
        help="Run just this check (repeatable): `sql` (unreferenced .sql files) or `routes` (backend routes nothing "
        "calls; imports the app, so it is the slow one). Default: every check that is configured.",
    ),
    update_baseline: bool = typer.Option(
        False,
        "--update-baseline",
        help="Routes only: rewrite each app's `baseline` file with the routes currently uncalled (and report nothing). "
        "Use once to adopt the check, then only to remove lines.",
    ),
) -> None:
    """Dead code that is invisible to a linter: `.sql` files no Python code loads (`sql_roots`) and backend
    (FastAPI) routes that neither non-generated frontend code nor a `url_for(...)` call references (the `apps`
    array of tables). Both are configured under tool.bdt.dead_code in pyproject.toml; generated API-client code and
    tests never count as callers. Exit 0 clean, 1 findings, 2 setup error.
    """
    root = Path.cwd()
    pyproject = find_pyproject(root)
    repo_root = pyproject.parent if pyproject else root
    try:
        findings = dead_code_mod.run(load_bdt_table("dead_code", root), repo_root=repo_root, only=only or [], update_baseline=update_baseline)
    except (api_usage_mod.ApiUsageError, tomllib.TOMLDecodeError) as exc:
        typer.echo(f"bdt dead-code: {exc}", err=True)
        raise typer.Exit(2) from exc
    if update_baseline:
        typer.echo("bdt dead-code: baseline(s) updated")
        raise typer.Exit(0)
    if not findings:
        typer.echo("bdt dead-code: no dead code found")
        raise typer.Exit(0)
    typer.echo(render_findings(findings))
    typer.echo(f"\n{len(findings)} issue(s) found.")
    raise typer.Exit(1)


@app.command("find-injection")
def find_injection_cmd(
    paths: list[str] = typer.Argument(None, help="Files and/or directories to scan (default: current directory, recursive)."),
    diff: bool = typer.Option(False, "--diff", help="Scan only files changed on this branch vs --base, plus uncommitted and untracked files."),
    base: str = typer.Option(None, "--base", help="Branch/ref to diff against with --diff (default: origin's default branch)."),
    strict: bool = typer.Option(False, "--strict", help="Also exit 1 when there are items to review (default: only definite errors fail)."),
    exclude: list[str] = typer.Option(
        None,
        "--exclude",
        help="Directory name (any depth), root-relative path, or glob to skip (repeatable), e.g. --exclude vendor --exclude 'assets/**/*.js'. "
        "Also configurable via [tool.bdt.lint] exclude_dirs in pyproject.toml.",
    ),
    no_gitignore: bool = typer.Option(False, "--no-gitignore", help="Also scan files ignored by .gitignore (default: they are skipped)."),
) -> None:
    """Find injection risks (bmsuisse/devtools#54) in backend and frontend code: SQL built from f-strings/
    concatenation or from an unverifiable function call, eval/exec/shell/unsafe deserialization in Python,
    innerHTML/dangerouslySetInnerHTML/eval/new Function/un-sandboxed iframes in the frontend, and a missing
    or weakened Content-Security-Policy. Definite problems are errors; the rest are listed for a human/AI to verify.
    """
    result = find_injection_mod.run(paths or [], diff=diff, base=base, exclude=exclude or [], respect_gitignore=not no_gitignore)
    raise typer.Exit(find_injection_mod.print_report(result, strict=strict))


@translate_app.callback(invoke_without_command=True)
def translate_cmd(
    ctx: typer.Context,
    check: bool = typer.Option(
        False, "--check", help="Don't write anything; exit 1 if code uses keys missing from translations.toml or generated files are stale"
    ),
    import_json: bool = typer.Option(
        False, "--import", help="One-time migration: merge existing <lng>.json files into translations.toml first"
    ),
) -> None:
    """Generate <lng>.json files from the single translations.toml (configured in `[tool.bdt.translate]`).

    Keys used in code (`t("KEY")`, `"KEY" | tr`) but missing from translations.toml are appended
    with an English placeholder and the run fails until real translations are filled in. Generated
    JSON files should be git-ignored.
    """
    if ctx.invoked_subcommand is not None:
        return
    cfg = translate_mod.load_config()
    if not cfg.output and not check:
        typer.echo("No `output` configured under [tool.bdt.translate] in pyproject.toml", err=True)
        raise typer.Exit(2)
    try:
        res = translate_mod.run(cfg, check=check, import_json=import_json)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    if res.imported:
        typer.echo(f"Imported {res.imported} value(s) from existing JSON files")
    for key in res.new_keys:
        typer.echo(f"{'missing' if check else 'added'}: {key}", err=True)
    for key, langs in res.incomplete.items():
        typer.echo(f"warning: {key} has no {', '.join(langs)} (falls back to de/en)", err=True)
    for p in res.stale:
        typer.echo(f"stale: {p} (run `bdt translate`)", err=True)
    if res.stale:
        raise typer.Exit(1)
    if res.new_keys:
        typer.echo(
            "Keys used in code are missing from translations.toml."
            if check
            else "New keys found in code. Add their translations to translations.toml and rerun.",
            err=True,
        )
        raise typer.Exit(1)
    if not check:
        typer.echo(f"Wrote {len(res.written)} file(s)")


@env_app.command("get-keys")
def env_get_keys(
    search: str | None = typer.Option(None, "--search", "-s", help="Only keys containing this text (case-insensitive)"),
    no_home: bool = typer.Option(False, "--no-home", help="Don't also list the env files directly in ~"),
) -> None:
    """List variable names (never values) from .env, *.env and .env.* files under the current directory (and in ~).

    Skips node_modules, .git, .venv, .worktrees and .claude.
    """
    home = None if no_home else Path.home()
    cwd = Path.cwd()
    files = env_files_mod.get_keys(cwd, home, search)
    if not files:
        typer.echo("No matching env files/keys found.", err=True)
        raise typer.Exit(1)
    typer.echo(env_files_mod.format_keys(files, home, cwd))


@translate_app.command("add")
def translate_add(
    key: str = typer.Argument(..., help="Translation key, e.g. ADD_BUTTON"),
    texts: list[str] = typer.Argument(..., metavar="LANG=TEXT...", help="Translations, e.g. en=Add de=Hinzufügen fr=Ajouter it=Aggiungi"),
    force: bool = typer.Option(False, "--force", help="Overwrite the key if it already exists"),
) -> None:
    """Add KEY with its translations to translations.toml and regenerate all <lng>.json files."""
    values: dict[str, str] = {}
    for t in texts:
        lng, sep, text = t.partition("=")
        if not sep or not lng:
            typer.echo(f"Expected LANG=TEXT, got {t!r}", err=True)
            raise typer.Exit(2)
        values[lng] = text
    cfg = translate_mod.load_config()
    try:
        res = translate_mod.add_key(cfg, key, values, force=force)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    missing = [lng for lng in cfg.languages if lng not in values]
    if missing:
        typer.echo(f"warning: no {', '.join(missing)} translation given (falls back to de/en)", err=True)
    for k in res.new_keys:
        typer.echo(f"added placeholder for key used in code: {k}", err=True)
    typer.echo(f"Added {key}; wrote {len(res.written)} file(s)")


@app.command("find-repo")
def find_repo_cmd(
    name: str,
    root: Path | None = typer.Option(
        None,
        "--root",
        help="Local work dir to search (default: $AZDO_WORK_DIR/$BMS_WORK_DIR, falling back to ~/projects, or C:/Projects on Windows)",
    ),
    org: str | None = typer.Option(
        None, "--org", envvar=["AZDO_ORG", "BMS_ORG"], help="Azure DevOps org to search when there's no local match"
    ),
    github_org: str | None = typer.Option(
        None,
        "--github-org",
        envvar=["GITHUB_ORG", "BMS_GITHUB_ORG"],
        help="GitHub org to search when there's no local match",
    ),
    yes: bool = typer.Option(False, "--yes", help="Clone a remote-only match without an interactive confirmation prompt"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT for cloning over HTTPS (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Find a repo by name: locally first, then in an Azure DevOps org and/or a GitHub org if there's
    no local match -- offering to clone it.

    Replaces the `cross-repo-discovery` skill's full `ALL_REPOS.md` org sync with a
    single-name lookup; it reads the same AZDO_WORK_DIR/BMS_WORK_DIR and AZDO_ORG/BMS_ORG
    env vars that skill used, so switching over needs no reconfiguration. A GitHub-org match
    clones into `root/github/<repo>`, by convention.
    """
    find_repo_mod.run(name, root=root, org=org, github_org=github_org, yes=yes, pat=pat)


@cleanup_app.command("worktrees")
def cleanup_worktrees(
    root: Path = typer.Argument(Path("."), help="Root folder to scan for git repositories (recursively)"),
    remote: str = typer.Option("origin", "--remote", help="Remote name whose main/test/dev branches count as 'merged into' (falls back to local main/test/dev if no such remote refs exist)"),
    keep_dbs: bool = typer.Option(False, "--keep-dbs", help="Don't drop a removed worktree's pgdevkit test DB(s) along with it"),
    yes: bool = typer.Option(False, "--yes", help="Actually remove; without this, only prints what would be removed"),
    pg_host: str = _PG_HOST_OPTION,
    pg_port: int = _PG_PORT_OPTION,
    pg_user: str | None = _PG_USER_OPTION,
) -> None:
    """Find and remove git worktrees fully merged into main/test/dev (and their pgdevkit test DB(s)), across every repo under root."""
    worktree_mod.clean_worktrees(
        root, remote=remote, keep_dbs=keep_dbs, yes=yes, pg_host=pg_host, pg_port=pg_port, pg_user=pg_user or pgdevkit_constants.USER
    )


@cleanup_app.command("orphaned-dbs")
def cleanup_orphaned_dbs(
    root: Path = typer.Argument(Path("."), help="Root folder to scan for git repositories (recursively)"),
    include_caution: bool = typer.Option(
        False, "--include-caution", help="Also drop DBs flagged as possibly a standing reference DB (verify those first!)"
    ),
    yes: bool = typer.Option(False, "--yes", help="Actually drop; without this, only prints what would be dropped"),
    pg_host: str = _PG_HOST_OPTION,
    pg_port: int = _PG_PORT_OPTION,
    pg_user: str | None = _PG_USER_OPTION,
) -> None:
    """Find and drop pgdevkit test DBs whose worktree is already gone (e.g. removed by hand before this command existed)."""
    worktree_mod.clean_orphaned_dbs(
        root, include_caution=include_caution, yes=yes, pg_host=pg_host, pg_port=pg_port, pg_user=pg_user or pgdevkit_constants.USER
    )


@cleanup_app.command("db")
def cleanup_db(
    root: Path = typer.Argument(Path("."), help="Worktree/repo whose own pgdevkit test DB(s) to drop (default: current directory)"),
    confirm: bool = typer.Option(False, "--confirm", help="Drop without an interactive confirmation prompt"),
    pg_host: str = _PG_HOST_OPTION,
    pg_port: int = _PG_PORT_OPTION,
    pg_user: str | None = _PG_USER_OPTION,
) -> None:
    """Drop this worktree's own pgdevkit test DB(s), without touching the worktree itself.

    Meant to be run from inside a worktree (default root is '.'). Always requires an
    explicit confirmation -- pass --confirm to skip the interactive prompt.
    """
    worktree_mod.clean_current_db(
        root.resolve(), confirm=confirm, pg_host=pg_host, pg_port=pg_port, pg_user=pg_user or pgdevkit_constants.USER
    )


@cleanup_app.command("worktree")
def cleanup_worktree(
    path: Path = typer.Argument(Path("."), help="Worktree to remove (default: current directory)"),
    keep_db: bool = typer.Option(False, "--keep-db", help="Don't drop this worktree's pgdevkit test DB(s) along with it"),
    confirm: bool = typer.Option(False, "--confirm", help="Remove without an interactive confirmation prompt"),
    pg_host: str = _PG_HOST_OPTION,
    pg_port: int = _PG_PORT_OPTION,
    pg_user: str | None = _PG_USER_OPTION,
) -> None:
    """Remove this one worktree (and, unless --keep-db, its pgdevkit test DB(s)) -- regardless of merge status.

    Meant to be run from inside the worktree to remove (default path is '.'). Refuses to touch
    the main checkout, a protected branch (main/test/dev), a locked worktree, or a dirty one. Always
    requires an explicit confirmation -- pass --confirm to skip the interactive prompt.
    """
    worktree_mod.clean_current_worktree(
        path, confirm=confirm, keep_db=keep_db, pg_host=pg_host, pg_port=pg_port, pg_user=pg_user or pgdevkit_constants.USER
    )


def _draft_feat_pr(target: str | None, pat: str | None) -> None:
    """A 'feat' commit landing on an already-published PR forces it back to draft --
    a feature needs a fresh review pass before CI/merge, not just whatever review
    happened before this commit existed.

    Best-effort: swallows failures (no PR yet is the common case mid-implementation,
    plus auth/network hiccups) rather than turning a successful commit+push into a
    failure over a step that's purely a safety nudge.
    """
    try:
        remote = current_remote()
        if isinstance(remote, GitHubRemote):
            converted = gh_pr.set_draft(require_gh(), target=target)
        else:
            session = requests.Session()
            session.headers.update(auth_header(pat))
            pr = pr_build.get_pr(session, remote, current_branch(), target)
            converted = pr_build.set_draft(session, remote, pr)
    except (Exception, SystemExit):
        return
    if converted:
        print("\n'feat' commit pushed -- PR converted back to draft (needs review before CI runs). Run `bdt pr publish` when ready.")


@app.command()
def commit(
    message: str,
    files: list[str],
    json_output: bool = typer.Option(False, "--json", help="Structured JSON output for AI-agent callers"),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip pre-commit hooks (not intended for regular use; prints a warning)"),
    subrepo: list[str] = typer.Option([], "--subrepo", help="Submodule directory name to split matching files into (repeatable)"),
    skip_message_check: bool = typer.Option(False, "--skip-message-check", help="Don't require a conventional-commit-style message"),
    allow_main: bool = typer.Option(False, "--allow-main", help="Allow committing directly on main/master"),
    target: str | None = typer.Option(None, "--target", help=_LOOKUP_TARGET_HELP + " (the PR to draft on a 'feat' commit)"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else uses DefaultAzureCredential, e.g. your `az login`)",
    ),
) -> None:
    """Stage, commit, and push files, with pre-flight checks and a pre-commit-hook retry."""
    result = commit_mod.commit_and_push(
        message,
        files,
        no_verify=no_verify,
        require_message_quality=not skip_message_check,
        require_feature_branch=not allow_main,
        subrepos=subrepo,
    )
    if result.success and result.pushed and result.extra.get("commit_type") == "feat":
        _draft_feat_pr(target, pat)
    commit_mod.emit(result, use_json=json_output)


@logs_app.command("roles")
def logs_roles(
    resource_group: str = typer.Option(..., "--resource-group", envvar="AZURE_RESOURCE_GROUP"),
    app_insights: str = typer.Option(..., "--app-insights", envvar="AZURE_APP_INSIGHTS"),
    minutes: int = typer.Option(30, "--minutes"),
) -> None:
    """List cloud_RoleName values seen in the last N minutes (to pick a --role for `logs tail`)."""
    logs_mod.print_roles(app_insights, resource_group, minutes)


@logs_app.command("tail")
def logs_tail(
    role: str = typer.Option(..., "--role", help="cloud_RoleName to filter; 'all' for no filter"),
    resource_group: str = typer.Option(..., "--resource-group", envvar="AZURE_RESOURCE_GROUP"),
    app_insights: str = typer.Option(..., "--app-insights", envvar="AZURE_APP_INSIGHTS"),
    minutes: int = typer.Option(30, "--minutes"),
    level: str = typer.Option("verbose", "--level", help=f"Minimum severity: {', '.join(logs_mod.SEVERITY_MAP)}"),
    no_color: bool = typer.Option(False, "--no-color"),
) -> None:
    """Fetch recent traces/exceptions for a role from Application Insights."""
    logs_mod.print_logs(app_insights, resource_group, minutes, level, role, no_color)


@logs_app.command("fetch")
def logs_fetch(
    env: str = typer.Option(..., "--env", help="Named environment configured under tool.bdt.envs in pyproject.toml"),
    out: Path = typer.Option(Path("logs"), "--out", help="Output directory for the extracted error log"),
    keep_archive: bool = typer.Option(False, "--keep-archive", help="Keep the downloaded .zip instead of deleting it"),
) -> None:
    """Download an App Service log archive and extract error/warning lines."""
    cfg = env_config.resolve_env(env)
    app_service_logs.fetch(cfg["webapp"], cfg["resource_group"], cfg.get("slot"), out, keep_archive=keep_archive)


if __name__ == "__main__":
    app()
