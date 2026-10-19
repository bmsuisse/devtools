"""Long PR titles/descriptions must reach the host intact: `bdt pr create` sends them in full
over the REST API, and a description the host stored shorter than sent is reported rather than lost silently."""

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bmsdna.devtools import pr_build
from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote

runner = CliRunner()
REMOTE = AdoRemote("bmeurope", "Proj", "Repo")
LONG = "".join(f"line {i}: some description text\n" for i in range(120))  # ~3.7k chars
assert 450 < len(LONG) < pr_build.ADO_PR_DESCRIPTION_MAX


def _ok(body: dict | None = None) -> MagicMock:
    r = MagicMock(status_code=200)
    r.json.return_value = body if body is not None else {}
    return r


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


def _ado_cli(monkeypatch, *, stub_create: bool = True) -> list[dict]:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda session, remote, source_branch, target: {"pullRequestId": 9, "title": "t", "description": ""})
    created: list[dict] = []

    def fake_create(session, remote, source, target, **kwargs):
        created.append(kwargs)
        return {"pullRequestId": 9, "title": kwargs.get("title") or "t"}

    if stub_create:
        monkeypatch.setattr("bmsdna.devtools.pr_build.create_pr", fake_create)
    return created


def test_ado_pr_create_sends_title_and_description_in_full(monkeypatch) -> None:
    created = _ado_cli(monkeypatch)
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--title", "My title", "--description", LONG])
    assert result.exit_code == 0, result.output
    assert created[0]["title"] == "My title"
    assert created[0]["description"] == LONG


def test_ado_pr_create_passthrough_title_and_description(monkeypatch) -> None:
    created = _ado_cli(monkeypatch)
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--", "--title", "T2", "--description", LONG])
    assert result.exit_code == 0, result.output
    assert created[0]["title"] == "T2"
    assert created[0]["description"] == LONG


def test_ado_pr_create_rejects_unknown_passthrough_flags(monkeypatch) -> None:
    created = _ado_cli(monkeypatch)
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--", "--frobnicate"])
    assert result.exit_code != 0
    assert created == []


def test_ado_pr_create_over_limit_description_is_a_clean_failure_without_a_link(monkeypatch) -> None:
    _ado_cli(monkeypatch, stub_create=False)
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--description", "x" * 4001])
    assert result.exit_code == 1
    assert "at most 4000" in result.output
    assert "pullrequest/" not in result.output


def test_create_pr_refuses_description_over_limit_without_posting() -> None:
    session = MagicMock()
    with pytest.raises(SystemExit, match="at most 4000"):
        pr_build.create_pr(session, REMOTE, "f", "dev", title="t", description="x" * 4001)
    session.post.assert_not_called()


def test_create_pr_network_failure_hints_that_the_pr_may_exist() -> None:
    import requests

    session = MagicMock()
    session.post.side_effect = requests.ReadTimeout("slow")
    with pytest.raises(SystemExit, match="may have been created.*bdt pr info"):
        pr_build.create_pr(session, REMOTE, "f", "dev", title="t")


def test_create_pr_html_sign_in_page_is_reported_as_rejected_credentials() -> None:
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=203, reason="Non-Authoritative")
    session.post.return_value.json.side_effect = ValueError("not json")
    with pytest.raises(SystemExit, match="credentials rejected"):
        pr_build.create_pr(session, REMOTE, "f", "dev", title="t")


def test_create_pr_posts_refs_draft_labels_and_commit_subject_title(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.pr_build.head_commit_subject", lambda: "feat: widgets")
    session = MagicMock()
    session.post.return_value = _ok({"pullRequestId": 5})
    pr_build.create_pr(session, REMOTE, "feature-x", "dev", draft=True, labels=["bug"], description="d")
    assert session.post.call_args.args[0].endswith("/_apis/git/repositories/Repo/pullrequests")
    assert session.post.call_args.kwargs["json"] == {
        "sourceRefName": "refs/heads/feature-x",
        "targetRefName": "refs/heads/dev",
        "title": "feat: widgets",
        "isDraft": True,
        "description": "d",
        "labels": [{"name": "bug"}],
    }


def test_create_pr_exits_with_ado_message_on_error() -> None:
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=409, reason="Conflict")
    session.post.return_value.json.return_value = {"message": "An active PR already exists"}
    with pytest.raises(SystemExit, match="already exists"):
        pr_build.create_pr(session, REMOTE, "feature-x", "dev", title="t")


def test_ado_update_sends_full_title_and_description() -> None:
    session = MagicMock()
    session.patch.return_value = _ok()
    title = "T" * 300
    pr_build.update(session, REMOTE, {"pullRequestId": 3, "title": "old", "description": ""}, title, LONG)
    assert session.patch.call_args.kwargs["json"] == {"title": title, "description": LONG}


def test_ado_pr_create_applies_az_flags_and_overrides(monkeypatch) -> None:
    created = _ado_cli(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.cli.webbrowser.open", opened.append)
    seen: dict = {}

    def fake_create(session, remote, source, target, **kwargs):
        seen.update(remote=remote, source=source, target=target, **kwargs)
        return {"pullRequestId": 9, "title": "t"}

    monkeypatch.setattr("bmsdna.devtools.pr_build.create_pr", fake_create)
    result = runner.invoke(
        app,
        ["pr", "create", "--target", "dev", "--", "--org", "https://dev.azure.com/other", "-p", "P2", "-r", "R2",
         "-s", "refs/heads/topic", "--target-branch", "main", "--draft", "false", "--labels", "x", "y",
         "--work-items", "5", "--reviewers", "a@x.com", "--squash", "--auto-complete", "--open"],
    )
    assert result.exit_code == 0, result.output
    assert (seen["remote"].org, seen["remote"].project, seen["remote"].repo) == ("other", "P2", "R2")
    assert (seen["source"], seen["target"], seen["draft"]) == ("topic", "main", False)
    assert seen["labels"] == ["x", "y"]
    assert seen["options"].reviewers == ["a@x.com"] and seen["options"].squash and seen["options"].auto_complete
    assert seen["work_item_ids"] == [5]  # --work-items shares the --issue path
    assert opened == ["https://dev.azure.com/other/P2/_git/R2/pullrequest/9"]
    assert created == []


def test_ado_pr_create_dry_run_prints_the_body_and_creates_nothing(monkeypatch) -> None:
    _ado_cli(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.pr_build.create_pr", lambda *a, **k: pytest.fail("must not create"))
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--title", "T", "--", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert '"targetRefName": "refs/heads/dev"' in result.output and '"title": "T"' in result.output


def test_ado_pr_create_notes_flags_it_cannot_honour(monkeypatch) -> None:
    _ado_cli(monkeypatch)
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--", "--milestone", "v1"])
    assert result.exit_code == 0, result.output
    assert "--milestone is ignored on Azure DevOps" in result.output


def test_ado_pr_create_body_file(monkeypatch, tmp_path) -> None:
    created = _ado_cli(monkeypatch)
    f = tmp_path / "pr.md"
    f.write_text("body from file", encoding="utf-8")
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--", "--body-file", str(f)])
    assert result.exit_code == 0, result.output
    assert created[0]["description"] == "body from file"
