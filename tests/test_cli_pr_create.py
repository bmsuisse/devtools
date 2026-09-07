"""`bdt pr create` must pass the parsed org/project/repo through to
`az repos pr create` explicitly, rather than relying on `az`'s own remote
auto-detection -- that auto-detect doesn't understand the SSH `v3` remote
form at all, and separately, an `az devops configure -d project=...` default
that happens to point at a *different* ADO project than the current repo's
own silently makes `az` look for the PR's repository in the wrong project.
"""

from unittest.mock import MagicMock

from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote

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
