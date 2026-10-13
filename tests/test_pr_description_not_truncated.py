"""Long PR titles/descriptions must reach the host intact: `bdt pr create` applies them itself
(REST on Azure DevOps, `--body-file -` on GitHub) instead of squeezing them through `az`'s
`--description` (which has been seen cutting long text at ~450 characters), and a description the
host stored shorter than sent is reported rather than lost silently."""

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bmsdna.devtools import pr_build
from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote

runner = CliRunner()
REMOTE = AdoRemote("bmeurope", "Proj", "Repo")
LONG = "".join(f"line {i}: some description text\n" for i in range(120))  # ~3.7k chars
assert 450 < len(LONG) < pr_build.ADO_PR_DESCRIPTION_MAX


def _ok(body: dict | None = None) -> MagicMock:
    r = MagicMock(status_code=200)
    r.json.return_value = body if body is not None else {}
    return r


def test_split_az_text_args_extracts_title_and_description() -> None:
    rest, title, desc = pr_build.split_az_text_args(["--title", "T", "--description", "a", "b", "--reviewers", "x", "-d=c"])
    assert rest == ["--reviewers", "x"]
    assert title == "T"
    assert desc == "c"  # last one wins
    rest, _, desc = pr_build.split_az_text_args(["--description", "a", "b", "--open"])
    assert rest == ["--open"]
    assert desc == "a\nb"
    assert pr_build.split_az_text_args(["--description=x=y"]) == ([], None, "x=y")
    assert pr_build.split_az_text_args(["--bypass-policy"]) == (["--bypass-policy"], None, None)


def test_patch_pr_sends_full_description_and_warns_when_host_cuts_it(capsys) -> None:
    session = MagicMock()
    session.patch.return_value = _ok({"description": LONG[:450]})
    pr_build._patch_pr(session, REMOTE, 5, {"description": LONG})
    assert session.patch.call_args.kwargs["json"] == {"description": LONG}
    assert "cut short (450 of" in capsys.readouterr().err


def test_patch_pr_silent_when_stored_intact(capsys) -> None:
    session = MagicMock()
    session.patch.return_value = _ok({"description": LONG})
    pr_build._patch_pr(session, REMOTE, 5, {"description": LONG})
    assert capsys.readouterr().err == ""


def test_patch_pr_refuses_description_over_ado_limit() -> None:
    session = MagicMock()
    with pytest.raises(SystemExit) as e:
        pr_build._patch_pr(session, REMOTE, 5, {"description": "x" * (pr_build.ADO_PR_DESCRIPTION_MAX + 1)})
    assert "at most 4000" in str(e.value)
    session.patch.assert_not_called()


def _ado_cli(monkeypatch, patched: list[dict]) -> list[str]:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda session, remote, source_branch, target: {"pullRequestId": 9, "title": "t", "description": ""})
    monkeypatch.setattr("bmsdna.devtools.pr_build._patch_pr", lambda session, remote, pr_id, fields: patched.append(fields) or {})
    captured: list[str] = []

    def fake_run(cmd, **kwargs):
        captured[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps({"pullRequestId": 9, "title": "t"}), stderr="")

    monkeypatch.setattr("bmsdna.devtools.cli.subprocess.run", fake_run)
    return captured


def test_ado_pr_create_description_option_is_set_via_rest_not_az(monkeypatch) -> None:
    patched: list[dict] = []
    cmd = _ado_cli(monkeypatch, patched)
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--title", "My title", "--description", LONG])
    assert result.exit_code == 0, result.output
    assert "--description" not in cmd
    assert cmd[cmd.index("--title") + 1] == "My title"
    assert patched == [{"description": LONG}]


def test_ado_pr_create_passthrough_description_is_set_via_rest_not_az(monkeypatch) -> None:
    patched: list[dict] = []
    cmd = _ado_cli(monkeypatch, patched)
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--", "--title", "T2", "--description", LONG, "--open"])
    assert result.exit_code == 0, result.output
    assert LONG not in cmd and "--description" not in cmd
    assert cmd[cmd.index("--title") + 1] == "T2"
    assert "--open" in cmd
    assert patched == [{"description": LONG}]


def test_ado_pr_create_rejects_description_over_limit_before_creating(monkeypatch) -> None:
    patched: list[dict] = []
    cmd = _ado_cli(monkeypatch, patched)
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--description", "x" * 4001])
    assert result.exit_code != 0
    assert cmd == []  # `az` never ran, so no PR with a wrong description was left behind


def test_ado_update_sends_full_title_and_description() -> None:
    session = MagicMock()
    session.patch.return_value = _ok()
    title = "T" * 300
    pr_build.update(session, REMOTE, {"pullRequestId": 3, "title": "old", "description": ""}, title, LONG)
    assert session.patch.call_args.kwargs["json"] == {"title": title, "description": LONG}


def test_github_pr_create_pipes_full_description_on_stdin(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GitHubRemote("owner", "repo"))
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr_body", lambda gh: (7, ""))
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)
    big = LONG * 20  # well past what a Windows command line could carry
    result = runner.invoke(app, ["pr", "create", "--target", "main", "--title", "Long title " * 20, "--description", big])
    assert result.exit_code == 0, result.output
    cmd, kwargs = next(c for c in calls if "create" in c[0])
    assert cmd[cmd.index("--title") + 1] == "Long title " * 20
    assert cmd[cmd.index("--body-file") + 1] == "-"
    assert kwargs["input"] == big
    assert big not in cmd
