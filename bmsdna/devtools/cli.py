from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path

import requests
import typer
from pgdevkit.testdb import constants as pgdevkit_constants

from . import ado_issue, app_service_logs, commit as commit_mod
from . import env_config
from . import find_repo as find_repo_mod
from . import gh_issue, gh_pr
from . import logs as logs_mod
from . import pr_build, pr_issue_link, pr_labels, worktree as worktree_mod
from .ado_auth import auth_header
from .cli_tools import detect_agent_session, require_az, require_gh
from .gitrepo import AdoRemote, GitHubRemote, current_branch, current_remote

# Non-ASCII output (checkmarks, en-dashes in ADO project names, etc.) needs a
# UTF-8 stream — the default Windows console codepage isn't UTF-8, and would
# otherwise raise UnicodeEncodeError on the first ✓/✗ printed.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

__version__ = _pkg_version("bmsdna-devtools")

app = typer.Typer(
    name="bdt",
    help=f"Shared BMS developer tooling: PRs/builds (Azure DevOps or GitHub), worktrees, commits, logs (v{__version__})",
)


def _version_callback(value: bool) -> None:
    if value:
        print(f"bdt {__version__}")
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

issue_app = typer.Typer(name="issue", help="Issue / work item commands (Azure DevOps or GitHub, auto-detected from the git remote)")
app.add_typer(issue_app, name="issue")

issue_comment_app = typer.Typer(name="comment", help="Comment on an issue / work item")
issue_app.add_typer(issue_comment_app, name="comment")

logs_app = typer.Typer(name="logs", help="Application Insights / Log Analytics queries")
app.add_typer(logs_app, name="logs")

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


def _resolve_ado_pr(pat: str | None, remote: AdoRemote, source_branch: str, target: str) -> tuple[requests.Session, dict]:
    session = requests.Session()
    session.headers.update(auth_header(pat))
    pr = pr_build.get_pr(session, remote, source_branch, target)
    return session, pr


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


def _link_and_label_github(gh: str, remote: GitHubRemote, issue_numbers: list[int]) -> None:
    """Link every issue in `issue_numbers`, plus every issue `find_issue_refs_in_body` finds in
    the PR's actual body, to the current branch's PR, and label each `pr-available`.

    Best-effort per issue -- one bad/inaccessible issue number shouldn't stop the others from
    being linked/labeled; failures are collected and surfaced together to the caller (which
    wraps this in `_after_create`, so they're reported as a warning, not a failed `pr create`).
    """
    pr_number, body = gh_pr.get_pr_body(gh)
    all_numbers = list(dict.fromkeys([*issue_numbers, *pr_issue_link.find_issue_refs_in_body(body, remote)]))
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
    """
    all_numbers = list(dict.fromkeys([*issue_numbers, *pr_issue_link.find_issue_refs_in_body(description, remote)]))
    errors: list[str] = []
    for number in all_numbers:
        try:
            pr_build.link_work_item(session, remote, pr_id, number)
            ado_issue.add_pr_available_tag(session, remote, number)
        except requests.RequestException as e:
            errors.append(f"#{number}: {e}")
    if errors:
        raise RuntimeError("; ".join(errors))


@pr_app.command("create")
def pr_create(
    target: str = typer.Option("main", "--target", help="Target branch (e.g. main, test)"),
    draft: bool = typer.Option(
        True,
        "--draft/--no-draft",
        help="Create the PR as a draft (not ready for review). Defaults to draft; pass --no-draft to publish it immediately.",
    ),
    label: list[str] = typer.Option(
        [],
        "--label",
        help="Label to apply to the PR (repeatable). On GitHub the label must already exist on the repo "
        "(`gh label create`); Azure DevOps PR labels are freeform and created on the fly. "
        r"[tool.bdt.pr.required_labels] in pyproject.toml can require at least one label from each "
        "named group before the PR is created.",
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
        help="Azure DevOps PAT (else falls back to `az` login)",
    ),
    args: list[str] = typer.Argument(None, help="Extra args passed through to `az repos pr create` / `gh pr create`"),
) -> None:
    """Create a PR from the current branch into --target (Azure DevOps or GitHub, auto-detected)."""
    for path in screenshot:
        if not Path(path).is_file():
            raise typer.BadParameter(f"Screenshot not found: {path}", param_hint="--screenshot")
    for path in file:
        if not Path(path).is_file():
            raise typer.BadParameter(f"File not found: {path}", param_hint="--file")

    missing_groups = pr_labels.missing_label_groups(pr_labels.required_label_groups(), label)
    if missing_groups:
        raise typer.BadParameter(pr_labels.format_missing_groups_error(missing_groups), param_hint="--label")

    remote = current_remote()

    issue_numbers: list[int] = []
    for ref in issue:
        try:
            issue_numbers.append(pr_issue_link.parse_issue_ref(ref, remote))
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--issue") from e
    issue_numbers = list(dict.fromkeys(issue_numbers))

    source_branch = current_branch()

    pr_url: str | None = None
    if isinstance(remote, GitHubRemote):
        gh = require_gh()
        returncode, pr_url = gh_pr.create(gh, target, args or [], draft=draft, labels=label)
        build_policy = gh_pr.has_build_policy(gh, target)
        if returncode == 0 and (screenshot or file):
            _after_create(lambda: gh_pr.add_attachments(gh, remote.owner, remote.repo, source_branch, screenshot, file), "attaching screenshots/files")
        if returncode == 0:
            _after_create(lambda: _link_and_label_github(gh, remote, issue_numbers), "linking issue(s) / setting 'pr-available' label")
    else:
        az = require_az()
        cmd = [
            az, "repos", "pr", "create",
            "--organization", f"https://dev.azure.com/{remote.org}",
            "--project", remote.project,
            "--repository", remote.repo,
            "--target-branch", target,
            "--source-branch", source_branch,
            "--auto-complete", "false",
            "--output", "json",
            *(["--draft", "true"] if draft else []),
            *(["--labels", *label] if label else []),
            *(args or []),
        ]
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8")
        returncode = r.returncode
        if isinstance(r.stderr, str) and r.stderr.strip():
            print(r.stderr.strip(), file=sys.stderr)

        pr_json = None
        if returncode == 0 and isinstance(r.stdout, str):
            try:
                pr_json = json.loads(r.stdout)
            except json.JSONDecodeError:
                pr_json = None

        if pr_json is not None:
            # Concise summary instead of the raw `az` JSON blob — the web
            # link printed below is the part a human actually needs.
            print(f"PR #{pr_json.get('pullRequestId', '?')}: {pr_json.get('title', '?')}")
            try:
                pr_url = pr_build.pr_web_url(remote, pr_json["pullRequestId"])
            except (KeyError, TypeError):
                pr_url = None
        elif isinstance(r.stdout, str) and r.stdout.strip():
            print(r.stdout.rstrip())
        session = requests.Session()
        session.headers.update(auth_header(pat))
        build_policy = pr_build.has_build_policy(session, remote, target)
        if returncode == 0 and (detect_agent_session() is not None or screenshot or file):
            def _finish() -> None:
                pr = pr_build.get_pr(session, remote, source_branch, target)
                if screenshot or file:
                    pr = pr_build.add_attachments(session, remote, pr, screenshot, file)
                pr_build.ensure_session_note(session, remote, pr)

            _after_create(_finish, "attaching screenshots/files and/or noting the agent session")
        if returncode == 0 and pr_json is not None:
            _after_create(
                lambda: _link_and_label_ado(session, remote, pr_json["pullRequestId"], pr_json.get("description") or "", issue_numbers),
                "linking work item(s) / setting 'pr-available' tag",
            )

    if returncode == 0 and pr_url:
        print(f"\n{pr_url}")

    if returncode == 0 and draft:
        print("\nCreated as a draft PR. Run `bdt pr publish` to mark it ready for review.")

    if returncode == 0 and build_policy:
        print("\nRun `bdt pr status` to check whether the CI build passes.")

    raise typer.Exit(returncode)


@pr_app.command("publish")
def pr_publish(
    target: str = typer.Option("main", "--target", help="Target branch of the PR (Azure DevOps only)"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else falls back to `az` login)",
    ),
) -> None:
    """Mark the draft PR opened from the current branch as ready for review (Azure DevOps or GitHub, auto-detected)."""
    remote = current_remote()
    source_branch = current_branch()
    if isinstance(remote, GitHubRemote):
        gh_pr.publish(require_gh())
    else:
        session, pr = _resolve_ado_pr(pat, remote, source_branch, target)
        pr_build.publish(session, remote, pr)


@pr_app.command("status")
def pr_status(
    target_branch: str = typer.Option("main", "--target-branch", help="Target branch of the PR (Azure DevOps only — gh has no equivalent filter, it always resolves the PR for the current branch)"),
    wait: bool = typer.Option(False, "--wait", help="Poll until all pipelines/checks are completed; stops early and reports status if one needs manual approval"),
    pat: str | None = typer.Option(None, "--pat", envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"], help="Azure DevOps PAT (else falls back to `az` login)"),
) -> None:
    """Show build/check status for the PR opened from the current branch (Azure DevOps or GitHub, auto-detected)."""
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_pr.run(require_gh(), wait)
        return
    pr_build.run(remote, pat, target_branch, wait)


@pr_app.command("retry")
def pr_retry(
    target_branch: str = typer.Option("main", "--target-branch", help="Target branch of the PR (Azure DevOps only)"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else falls back to `az` login)",
    ),
) -> None:
    """Retry only the failed job(s)/stage(s) of the most recent build/run for the PR opened
    from the current branch (Azure DevOps or GitHub, auto-detected), instead of a full rerun.
    """
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_pr.retry(require_gh())
        return
    pr_build.retry(remote, pat, target_branch)


@pr_app.command("watch-deploy")
def pr_watch_deploy(
    target_branch: str = typer.Option("main", "--target-branch", help="Branch to watch for a directly-triggered build/workflow run (e.g. a post-merge deployment pipeline)"),
    wait: bool = typer.Option(False, "--wait", help="Poll until the build/workflow run(s) are completed; stops early and reports status if one needs manual approval"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else falls back to `az` login)",
    ),
) -> None:
    """Watch the most recent build/workflow run triggered directly on --target-branch -- e.g. a
    pipeline that only runs on the target branch once a PR merges into it and usually does the
    actual deployment -- as opposed to `bdt pr status`, which watches builds/checks tied to a PR
    (Azure DevOps or GitHub, auto-detected).
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
    target: str = typer.Option("main", "--target", help="Target branch of the PR (Azure DevOps only)"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else falls back to `az` login)",
    ),
) -> None:
    """Update the title/description of the PR opened from the current branch (Azure DevOps or GitHub, auto-detected)."""
    for path in screenshot:
        if not Path(path).is_file():
            raise typer.BadParameter(f"Screenshot not found: {path}", param_hint="--screenshot")
    for path in file:
        if not Path(path).is_file():
            raise typer.BadParameter(f"File not found: {path}", param_hint="--file")
    if title is None and description is None and not screenshot and not file:
        raise typer.BadParameter("Provide at least one of --title, --description, --screenshot, --file")

    remote = current_remote()
    source_branch = current_branch()
    if isinstance(remote, GitHubRemote):
        gh_pr.update(require_gh(), remote.owner, remote.repo, source_branch, title, description, screenshot, file)
    else:
        session, pr = _resolve_ado_pr(pat, remote, source_branch, target)
        pr_build.update(session, remote, pr, title, description, screenshot, file)


@pr_app.command("comment")
def pr_comment(
    message: str | None = typer.Option(None, "--message", help="Comment text"),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to embed in the comment (repeatable)"
    ),
    file: list[str] = typer.Option(
        [], "--file", help="Path to an arbitrary file to link in the comment as an attachment (repeatable)"
    ),
    target: str = typer.Option("main", "--target", help="Target branch of the PR (Azure DevOps only)"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else falls back to `az` login)",
    ),
) -> None:
    """Post a comment on the PR opened from the current branch (Azure DevOps or GitHub, auto-detected)."""
    for path in screenshot:
        if not Path(path).is_file():
            raise typer.BadParameter(f"Screenshot not found: {path}", param_hint="--screenshot")
    for path in file:
        if not Path(path).is_file():
            raise typer.BadParameter(f"File not found: {path}", param_hint="--file")
    if not message and not screenshot and not file:
        raise typer.BadParameter("Provide at least one of --message, --screenshot, --file")

    remote = current_remote()
    source_branch = current_branch()
    if isinstance(remote, GitHubRemote):
        gh_pr.comment_with_screenshots(require_gh(), remote.owner, remote.repo, source_branch, message, screenshot, file)
    else:
        session, pr = _resolve_ado_pr(pat, remote, source_branch, target)
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
    label: list[str] = typer.Option([], "--label", help="Label to apply (GitHub only, repeatable)"),
    tag: list[str] = typer.Option([], "--tag", help="Tag to apply (Azure DevOps only, repeatable)"),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to attach to the issue / work item (repeatable)"
    ),
    file: list[str] = typer.Option(
        [], "--file", help="Path to an arbitrary file to attach to the issue / work item as a linked attachment (repeatable)"
    ),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else falls back to `az` login)",
    ),
    args: list[str] = typer.Argument(None, help="Extra args passed through to `gh issue create` (GitHub only)"),
) -> None:
    """Create a new issue / work item (Azure DevOps or GitHub, auto-detected)."""
    for path in screenshot:
        if not Path(path).is_file():
            raise typer.BadParameter(f"Screenshot not found: {path}", param_hint="--screenshot")
    for path in file:
        if not Path(path).is_file():
            raise typer.BadParameter(f"File not found: {path}", param_hint="--file")

    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        resolved_board = gh_issue.resolve_board(board)
        gh_issue.create(
            require_gh(), remote.owner, remote.repo, title, description, label, screenshot, args or [], file_paths=file, board=resolved_board
        )
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        resolved_board = ado_issue.resolve_board(board)
        ado_issue.create(session, remote, type_, title, description, resolved_board, tag, screenshot, file)


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
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else falls back to `az` login)",
    ),
) -> None:
    """Search (or, with no keywords, just list) issues / work items, defaulting to open issues
    from the last 30 days (Azure DevOps or GitHub, auto-detected).
    """
    if state not in ("open", "closed", "all"):
        raise typer.BadParameter("Must be one of: open, closed, all", param_hint="--state")
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d") if since_days > 0 else None

    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        resolved_board = gh_issue.resolve_board(board)
        gh_issue.search(require_gh(), remote.owner, remote.repo, keywords or [], since, limit, state, board=resolved_board)
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        resolved_board = ado_issue.resolve_board(board)
        ado_issue.search(session, remote, keywords or [], since=since, board=resolved_board, top=limit, state=state)


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
    tag: list[str] | None = typer.Option(None, "--tag", help="Replaces all tags (Azure DevOps only, repeatable); omit to leave unchanged"),
    label: list[str] | None = typer.Option(None, "--label", help="Label to add (GitHub only, repeatable)"),
    remove_label: list[str] | None = typer.Option(None, "--remove-label", help="Label to remove (GitHub only, repeatable)"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else falls back to `az` login)",
    ),
) -> None:
    """Update an issue / work item's fields (Azure DevOps or GitHub, auto-detected)."""
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_issue.update(require_gh(), number, title, description, label, remove_label, state)
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        ado_issue.update(session, remote, number, title, description, board, tag, state)


@issue_app.command("delete")
def issue_delete(
    number: int = typer.Argument(..., help="Issue number (GitHub) or work item ID (Azure DevOps)"),
    yes: bool = typer.Option(False, "--yes", help="Confirm deletion without prompting"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else falls back to `az` login)",
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
        help="Azure DevOps PAT (else falls back to `az` login)",
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
        help="Azure DevOps PAT (else falls back to `az` login)",
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
        help="Azure DevOps PAT (else falls back to `az` login)",
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
        help="Azure DevOps PAT for cloning over HTTPS (else falls back to `az` login)",
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
    remote: str = typer.Option("origin", "--remote", help="Remote name whose main/test branches count as 'merged into' (falls back to local main/test if no such remote refs exist)"),
    keep_dbs: bool = typer.Option(False, "--keep-dbs", help="Don't drop a removed worktree's pgdevkit test DB(s) along with it"),
    yes: bool = typer.Option(False, "--yes", help="Actually remove; without this, only prints what would be removed"),
    pg_host: str = _PG_HOST_OPTION,
    pg_port: int = _PG_PORT_OPTION,
    pg_user: str | None = _PG_USER_OPTION,
) -> None:
    """Find and remove git worktrees fully merged into main/test (and their pgdevkit test DB(s)), across every repo under root."""
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
    the main checkout, a protected branch (main/test), a locked worktree, or a dirty one. Always
    requires an explicit confirmation -- pass --confirm to skip the interactive prompt.
    """
    worktree_mod.clean_current_worktree(
        path, confirm=confirm, keep_db=keep_db, pg_host=pg_host, pg_port=pg_port, pg_user=pg_user or pgdevkit_constants.USER
    )


def _draft_feat_pr(target: str, pat: str | None) -> None:
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
            converted = gh_pr.set_draft(require_gh())
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
    target: str = typer.Option("main", "--target", help="Target branch of the PR to draft on a 'feat' commit (Azure DevOps only)"),
    pat: str | None = typer.Option(
        None,
        "--pat",
        envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"],
        help="Azure DevOps PAT (else falls back to `az` login)",
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
