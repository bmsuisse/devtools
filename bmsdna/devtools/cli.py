from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import requests
import typer

from . import ado_issue, app_service_logs, commit as commit_mod
from . import env_config
from . import gh_issue, gh_pr
from . import logs as logs_mod
from . import pr_build, worktree as worktree_mod
from .ado_auth import auth_header
from .cli_tools import require_az, require_gh
from .gitrepo import AdoRemote, GitHubRemote, current_branch, current_remote

# Non-ASCII output (checkmarks, en-dashes in ADO project names, etc.) needs a
# UTF-8 stream — the default Windows console codepage isn't UTF-8, and would
# otherwise raise UnicodeEncodeError on the first ✓/✗ printed.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

app = typer.Typer(name="bdt", help="Shared BMS developer tooling: PRs/builds (Azure DevOps or GitHub), worktrees, commits, logs")

pr_app = typer.Typer(name="pr", help="Pull request commands (Azure DevOps or GitHub, auto-detected from the git remote)")
app.add_typer(pr_app, name="pr")

issue_app = typer.Typer(name="issue", help="Issue / work item commands (Azure DevOps or GitHub, auto-detected from the git remote)")
app.add_typer(issue_app, name="issue")

logs_app = typer.Typer(name="logs", help="Application Insights / Log Analytics queries")
app.add_typer(logs_app, name="logs")


def _resolve_ado_pr(pat: str | None, remote: AdoRemote, source_branch: str, target: str) -> tuple[requests.Session, dict]:
    session = requests.Session()
    session.headers.update(auth_header(pat))
    pr = pr_build.get_pr(session, remote, source_branch, target)
    return session, pr


def _attach_screenshots(attach: Callable[[], None]) -> None:
    """Run an attach-screenshots step without letting its failure mask an already-successful `pr create`.

    The PR itself is already live by the time this runs; a transient failure
    here (a rejected push, an attachment upload error, a stale --target not
    matching the PR ADO actually created) should surface as a warning, not
    flip the whole command's exit code or hide the fact that the PR exists.
    """
    try:
        attach()
    except (Exception, SystemExit) as e:
        print(f"Warning: PR created, but attaching screenshots failed: {e}")


@pr_app.command("create")
def pr_create(
    target: str = typer.Option("main", "--target", help="Target branch (e.g. main, test)"),
    draft: bool = typer.Option(False, "--draft", help="Create the PR as a draft (not ready for review)"),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to attach to the PR description (repeatable)"
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

    remote = current_remote()
    source_branch = current_branch()
    if isinstance(remote, GitHubRemote):
        gh = require_gh()
        returncode = gh_pr.create(gh, target, args or [], draft=draft)
        build_policy = gh_pr.has_build_policy(gh, target)
        if returncode == 0 and screenshot:
            _attach_screenshots(lambda: gh_pr.add_screenshots(gh, remote.owner, remote.repo, source_branch, screenshot))
        publish_hint = "`gh pr ready`"
    else:
        az = require_az()
        cmd = [
            az, "repos", "pr", "create",
            "--target-branch", target,
            "--source-branch", source_branch,
            "--auto-complete", "false",
            *(["--draft", "true"] if draft else []),
            *(args or []),
        ]
        returncode = subprocess.run(cmd).returncode
        session = requests.Session()
        session.headers.update(auth_header(pat))
        build_policy = pr_build.has_build_policy(session, remote, target)
        if returncode == 0 and screenshot:
            def _add() -> None:
                pr = pr_build.get_pr(session, remote, source_branch, target)
                pr_build.add_screenshots(session, remote, pr, screenshot)

            _attach_screenshots(_add)
        publish_hint = "`az repos pr update --id <PR-ID> --draft false`"

    if returncode == 0 and draft:
        print(f"\nCreated as a draft PR. Run `bdt pr publish` (or {publish_hint}) to mark it ready for review.")

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
    wait: bool = typer.Option(False, "--wait", help="Poll until all pipelines/checks are completed"),
    pat: str | None = typer.Option(None, "--pat", envvar=["AZURE_DEVOPS_EXT_PAT", "AZURE_DEVOPS_PAT"], help="Azure DevOps PAT (else falls back to `az` login)"),
) -> None:
    """Show build/check status for the PR opened from the current branch (Azure DevOps or GitHub, auto-detected)."""
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_pr.run(require_gh(), wait)
        return
    pr_build.run(remote, pat, target_branch, wait)


@pr_app.command("update")
def pr_update(
    title: str | None = typer.Option(None, "--title", help="New PR title"),
    description: str | None = typer.Option(
        None, "--description", help="New PR description (replaces the existing one)"
    ),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to append to the PR description (repeatable)"
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
    if title is None and description is None and not screenshot:
        raise typer.BadParameter("Provide at least one of --title, --description, --screenshot")

    remote = current_remote()
    source_branch = current_branch()
    if isinstance(remote, GitHubRemote):
        gh_pr.update(require_gh(), remote.owner, remote.repo, source_branch, title, description, screenshot)
    else:
        session, pr = _resolve_ado_pr(pat, remote, source_branch, target)
        pr_build.update(session, remote, pr, title, description, screenshot)


@pr_app.command("comment")
def pr_comment(
    message: str | None = typer.Option(None, "--message", help="Comment text"),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to embed in the comment (repeatable)"
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
    if not message and not screenshot:
        raise typer.BadParameter("Provide at least one of --message, --screenshot")

    remote = current_remote()
    source_branch = current_branch()
    if isinstance(remote, GitHubRemote):
        gh_pr.comment_with_screenshots(require_gh(), remote.owner, remote.repo, source_branch, message, screenshot)
    else:
        session, pr = _resolve_ado_pr(pat, remote, source_branch, target)
        pr_build.comment_with_screenshots(session, remote, pr["pullRequestId"], message, screenshot)


@issue_app.command("create")
def issue_create(
    title: str = typer.Option(..., "--title", help="Issue / work item title"),
    description: str | None = typer.Option(None, "--description", help="Issue / work item description body"),
    type_: str = typer.Option("Bug", "--type", help="Work item type, e.g. Bug, Task, User Story (Azure DevOps only)"),
    board: str | None = typer.Option(
        None,
        "--board",
        help="Azure Boards team to file the work item against — sets its Area Path so the item shows up on that "
        r"team's board (Azure DevOps only; parameter overrides \[tool.bdt.ado].board in pyproject.toml)",
    ),
    label: list[str] = typer.Option([], "--label", help="Label to apply (GitHub only, repeatable)"),
    tag: list[str] = typer.Option([], "--tag", help="Tag to apply (Azure DevOps only, repeatable)"),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to attach to the issue / work item (repeatable)"
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

    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_issue.create(require_gh(), remote.owner, remote.repo, title, description, label, screenshot, args or [])
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        resolved_board = ado_issue.resolve_board(board)
        ado_issue.create(session, remote, type_, title, description, resolved_board, tag, screenshot)


@issue_app.command("comment")
def issue_comment(
    number: int = typer.Argument(..., help="Issue number (GitHub) or work item ID (Azure DevOps)"),
    message: str | None = typer.Option(None, "--message", help="Comment text"),
    screenshot: list[str] = typer.Option(
        [], "--screenshot", help="Path to an image to embed in the comment (repeatable)"
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
    if not message and not screenshot:
        raise typer.BadParameter("Provide at least one of --message, --screenshot")

    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_issue.comment(require_gh(), remote.owner, remote.repo, number, message, screenshot)
    else:
        session = requests.Session()
        session.headers.update(auth_header(pat))
        ado_issue.comment_with_screenshots(session, remote, number, message, screenshot)


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
def commit(
    message: str,
    files: list[str],
    json_output: bool = typer.Option(False, "--json", help="Structured JSON output for AI-agent callers"),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip pre-commit hooks"),
    subrepo: list[str] = typer.Option([], "--subrepo", help="Submodule directory name to split matching files into (repeatable)"),
    skip_message_check: bool = typer.Option(False, "--skip-message-check", help="Don't require a conventional-commit-style message"),
    allow_main: bool = typer.Option(False, "--allow-main", help="Allow committing directly on main/master"),
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
