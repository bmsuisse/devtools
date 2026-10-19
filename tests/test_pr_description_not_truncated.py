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


def test_split_text_args_extracts_title_and_description() -> None:
    rest, title, desc = pr_build.split_text_args(["--title", "T", "--description", "a", "b", "--reviewers", "x", "-d=c"])
    assert rest == ["--reviewers", "x"]
    assert title == "T"
    assert desc == "c"  # last one wins
    rest, _, desc = pr_build.split_text_args(["--description", "a", "b", "--open"])
    assert rest == ["--open"]
    assert desc == "a\nb"
    assert pr_build.split_text_args(["--description=x=y"]) == ([], None, "x=y")
    assert pr_build.split_text_args(["--bypass-policy"]) == (["--bypass-policy"], None, None)


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


def _ado_cli(monkeypatch) -> list[dict]:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda session, remote, source_branch, target: {"pullRequestId": 9, "title": "t", "description": ""})
    created: list[dict] = []

    def fake_create(session, remote, source, target, **kwargs):
        created.append(kwargs)
        return {"pullRequestId": 9, "title": kwargs.get("title") or "t"}

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


def test_ado_pr_create_rejects_other_passthrough_args(monkeypatch) -> None:
    created = _ado_cli(monkeypatch)
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--", "--open"])
    assert result.exit_code != 0
    assert created == []


def test_ado_pr_create_rejects_description_over_limit_before_creating(monkeypatch) -> None:
    created = _ado_cli(monkeypatch)
    result = runner.invoke(app, ["pr", "create", "--target", "dev", "--description", "x" * 4001])
    assert result.exit_code != 0
    assert created == []  # no PR with a wrong description was left behind


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
