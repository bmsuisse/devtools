"""`bdt commit` must convert an already-published PR back to draft when the pushed
commit is a 'feat' (Conventional Commits type) -- a feature needs a fresh review
pass before CI/merge, not just whatever review happened before the commit existed.
"""

from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.commit import CommitResult
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote

runner = CliRunner()


def feat_result() -> CommitResult:
    return CommitResult(
        success=True, committed=True, pushed=True, message="feat(x): add widget",
        files=["a.txt"], commit_sha="abc1234", extra={"commit_type": "feat"},
    )


def fix_result() -> CommitResult:
    return CommitResult(
        success=True, committed=True, pushed=True, message="fix: correct bug",
        files=["a.txt"], commit_sha="abc1234", extra={"commit_type": "fix"},
    )


def test_commit_converts_github_pr_to_draft_on_feat_commit(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.commit_mod.commit_and_push", lambda *a, **k: feat_result())
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GitHubRemote("owner", "repo"))
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")

    set_draft_calls: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.set_draft", lambda gh: (set_draft_calls.append(gh), True)[1])

    result = runner.invoke(app, ["commit", "feat(x): add widget", "a.txt"])

    assert result.exit_code == 0, result.output
    assert set_draft_calls == ["gh"]
    assert "converted back to draft" in result.output


def test_commit_does_not_touch_pr_on_non_feat_commit(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.commit_mod.commit_and_push", lambda *a, **k: fix_result())
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GitHubRemote("owner", "repo"))

    called = []
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.set_draft", lambda gh: called.append(gh))

    result = runner.invoke(app, ["commit", "fix: correct bug", "a.txt"])

    assert result.exit_code == 0, result.output
    assert called == []
    assert "converted back to draft" not in result.output


def test_commit_swallows_failure_when_no_pr_exists_yet(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.commit_mod.commit_and_push", lambda *a, **k: feat_result())
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GitHubRemote("owner", "repo"))
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")

    def raise_no_pr(gh):
        raise SystemExit("No PR found")

    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.set_draft", raise_no_pr)

    result = runner.invoke(app, ["commit", "feat(x): add widget", "a.txt"])

    assert result.exit_code == 0, result.output
    assert "converted back to draft" not in result.output


def test_commit_converts_ado_pr_to_draft_on_feat_commit(monkeypatch) -> None:
    remote = AdoRemote("myorg", "MyProj", "myrepo")
    monkeypatch.setattr("bmsdna.devtools.cli.commit_mod.commit_and_push", lambda *a, **k: feat_result())
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})

    pr = {"pullRequestId": 42, "isDraft": False}
    monkeypatch.setattr("bmsdna.devtools.cli.pr_build.get_pr", lambda session, remote, source, target: pr)
    set_draft_calls: list[dict] = []
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.set_draft", lambda session, remote, pr: (set_draft_calls.append(pr), True)[1]
    )

    result = runner.invoke(app, ["commit", "feat(x): add widget", "a.txt"])

    assert result.exit_code == 0, result.output
    assert set_draft_calls == [pr]
    assert "converted back to draft" in result.output
