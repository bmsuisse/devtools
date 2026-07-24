from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

from . import app_service_logs, commit as commit_mod
from . import env_config
from . import gh_pr
from . import logs as logs_mod
from . import pr_build, worktree as worktree_mod
from .cli_tools import require_az, require_gh
from .gitrepo import GitHubRemote, current_branch, current_remote

# Non-ASCII output (checkmarks, en-dashes in ADO project names, etc.) needs a
# UTF-8 stream — the default Windows console codepage isn't UTF-8, and would
# otherwise raise UnicodeEncodeError on the first ✓/✗ printed.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

app = typer.Typer(name="bdt", help="Shared BMS developer tooling: PRs/builds (Azure DevOps or GitHub), worktrees, commits, logs")

pr_app = typer.Typer(name="pr", help="Pull request commands (Azure DevOps or GitHub, auto-detected from the git remote)")
app.add_typer(pr_app, name="pr")

logs_app = typer.Typer(name="logs", help="Application Insights / Log Analytics queries")
app.add_typer(logs_app, name="logs")


@pr_app.command("create")
def pr_create(
    target: str = typer.Option("main", "--target", help="Target branch (e.g. main, test)"),
    args: list[str] = typer.Argument(None, help="Extra args passed through to `az repos pr create` / `gh pr create`"),
) -> None:
    """Create a PR from the current branch into --target (Azure DevOps or GitHub, auto-detected)."""
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        raise typer.Exit(gh_pr.create(require_gh(), target, args or []))

    az = require_az()
    cmd = [
        az, "repos", "pr", "create",
        "--target-branch", target,
        "--source-branch", current_branch(),
        "--auto-complete", "false",
        *(args or []),
    ]
    raise typer.Exit(subprocess.run(cmd).returncode)


@pr_app.command("status")
def pr_status(
    target_branch: str = typer.Option("main", "--target-branch", help="Target branch of the PR (Azure DevOps only — gh has no equivalent filter, it always resolves the PR for the current branch)"),
    wait: bool = typer.Option(False, "--wait", help="Poll until all pipelines/checks are completed"),
    pat: str | None = typer.Option(None, "--pat", envvar="AZURE_DEVOPS_PAT", help="Azure DevOps PAT (else falls back to `az` login)"),
) -> None:
    """Show build/check status for the PR opened from the current branch (Azure DevOps or GitHub, auto-detected)."""
    remote = current_remote()
    if isinstance(remote, GitHubRemote):
        gh_pr.run(require_gh(), wait)
        return
    pr_build.run(remote, pat, target_branch, wait)


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
    app_service_logs.fetch(cfg["webapp"], cfg["resource_group"], cfg["slot"], out, keep_archive=keep_archive)


if __name__ == "__main__":
    app()
