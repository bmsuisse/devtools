"""`bdt pr create` must pass the parsed org/project/repo through to
`az repos pr create` explicitly, rather than relying on `az`'s own remote
auto-detection -- that auto-detect doesn't understand the SSH `v3` remote
form at all, and separately, an `az devops configure -d project=...` default
that happens to point at a *different* ADO project than the current repo's
own silently makes `az` look for the PR's repository in the wrong project.
"""

import json
from unittest.mock import MagicMock

from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote

runner = CliRunner()


def test_pr_create_passes_organization_project_repository_to_az(monkeypatch) -> None:
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0)

    monkeypatch.setattr("bmsdna.devtools.cli.subprocess.run", fake_run)
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)

    result = runner.invoke(app, ["pr", "create", "--target", "test"])

    assert result.exit_code == 0, result.output
    assert "--organization" in captured_cmd
    assert captured_cmd[captured_cmd.index("--organization") + 1] == "https://dev.azure.com/bmeurope"
    assert "--project" in captured_cmd
    assert captured_cmd[captured_cmd.index("--project") + 1] == "BMS - CCMT2"
    assert "--repository" in captured_cmd
    assert captured_cmd[captured_cmd.index("--repository") + 1] == "BMS - CCMT2"


def test_pr_create_passes_labels_and_prints_web_link_for_ado(monkeypatch) -> None:
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps({"pullRequestId": 456, "title": "feat: widgets"}), stderr="")

    monkeypatch.setattr("bmsdna.devtools.cli.subprocess.run", fake_run)

    result = runner.invoke(app, ["pr", "create", "--target", "test", "--label", "bug", "--label", "urgent"])

    assert result.exit_code == 0, result.output
    assert "--labels" in captured_cmd
    labels_idx = captured_cmd.index("--labels")
    assert captured_cmd[labels_idx + 1 : labels_idx + 3] == ["bug", "urgent"]
    assert "https://dev.azure.com/bmeurope/BMS%20-%20CCMT2/_git/BMS%20-%20CCMT2/pullrequest/456" in result.output
    assert "PR #456: feat: widgets" in result.output
    assert '"pullRequestId"' not in result.output  # the raw `az` JSON blob must not be dumped on success


def test_pr_create_passes_labels_and_prints_web_link_for_github(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--label", "bug"])

    assert result.exit_code == 0, result.output
    assert captured_cmd.count("--label") == 1
    assert captured_cmd[captured_cmd.index("--label") + 1] == "bug"
    assert "https://github.com/owner/repo/pull/7" in result.output


def test_pr_create_defaults_to_draft_and_prints_publish_hint_for_github(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    result = runner.invoke(app, ["pr", "create", "--target", "main"])

    assert result.exit_code == 0, result.output
    assert "--draft" in captured_cmd
    assert "https://github.com/owner/repo/pull/7" in result.output
    assert "bdt pr publish" in result.output
    assert "gh pr ready" in result.output


def test_pr_create_no_draft_skips_draft_flag_and_publish_hint_for_github(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--no-draft"])

    assert result.exit_code == 0, result.output
    assert "--draft" not in captured_cmd
    assert "https://github.com/owner/repo/pull/7" in result.output
    assert "bdt pr publish" not in result.output


def test_pr_create_defaults_to_draft_for_ado(monkeypatch) -> None:
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps({"pullRequestId": 456, "title": "feat: widgets"}), stderr="")

    monkeypatch.setattr("bmsdna.devtools.cli.subprocess.run", fake_run)

    result = runner.invoke(app, ["pr", "create", "--target", "test"])

    assert result.exit_code == 0, result.output
    assert "--draft" in captured_cmd
    assert captured_cmd[captured_cmd.index("--draft") + 1] == "true"
    assert "bdt pr publish" in result.output
    assert "az repos pr update --id <PR-ID> --draft false" in result.output


def test_pr_create_fails_before_touching_gh_az_when_required_label_group_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_labels.required_label_groups",
        lambda: {"risk": ["breaking", "non-breaking"]},
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not be reached — the missing-label check must run first")

    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", fail_if_called)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--label", "bug"])

    assert result.exit_code != 0
    assert "risk" in result.output
    assert "breaking" in result.output


def test_pr_create_proceeds_when_required_label_group_satisfied(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_labels.required_label_groups",
        lambda: {"risk": ["breaking", "non-breaking"]},
    )
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.subprocess.run",
        lambda cmd, **kwargs: MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr=""),
    )

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--label", "non-breaking"])

    assert result.exit_code == 0, result.output
