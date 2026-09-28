"""`--pr-id` lets `bdt pr status/retry/publish/update/comment` act on a specific PR
directly, instead of always resolving "the PR" from the current git branch -- these tests
check that the flag is actually threaded through to the ADO/GitHub backends, and that (on
the ADO side, where a PR is normally found by searching for a source/target branch pair)
`--pr-id` skips that branch-based lookup entirely, so it works even without that PR's
branch checked out locally.

`pr watch-deploy` deliberately has no `--pr-id` (it watches a branch-triggered
build/workflow run, not any particular PR) and isn't covered here.
"""

import pytest
from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote

runner = CliRunner()

ADO_REMOTE = AdoRemote("myorg", "MyProj", "myrepo")
GITHUB_REMOTE = GitHubRemote("owner", "repo")


def _fail_current_branch() -> str:
    raise AssertionError("must not need a checked-out branch when --pr-id is given")


# -- GitHub -----------------------------------------------------------------


def test_pr_status_with_pr_id_passes_it_to_gh_pr_run(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured = {}
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.run", lambda gh, wait, pr_id=None: captured.update(gh=gh, wait=wait, pr_id=pr_id))

    result = runner.invoke(app, ["pr", "status", "--pr-id", "99"])

    assert result.exit_code == 0, result.output
    assert captured == {"gh": "gh", "wait": False, "pr_id": 99}


def test_pr_retry_with_pr_id_passes_it_to_gh_pr_retry(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured = {}
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.retry", lambda gh, pr_id=None: captured.update(gh=gh, pr_id=pr_id))

    result = runner.invoke(app, ["pr", "retry", "--pr-id", "99"])

    assert result.exit_code == 0, result.output
    assert captured == {"gh": "gh", "pr_id": 99}


def test_pr_publish_with_pr_id_passes_it_to_gh_pr_publish(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured = {}
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.publish", lambda gh, pr_id=None: captured.update(gh=gh, pr_id=pr_id))

    result = runner.invoke(app, ["pr", "publish", "--pr-id", "99"])

    assert result.exit_code == 0, result.output
    assert captured == {"gh": "gh", "pr_id": 99}


def test_pr_update_with_pr_id_skips_the_redundant_branch_lookup(monkeypatch) -> None:
    """`gh_pr.update()` already re-resolves the branch itself from `--pr-id` (it needs the
    PR anyway, to fetch its current title/body) -- the CLI must not pay for a second,
    redundant `gh pr view` round-trip (`gh_pr.get_pr`) just to compute a `branch` value
    `update()` is going to overwrite internally anyway.
    """
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr(
        "bmsdna.devtools.cli.gh_pr.get_pr", lambda gh, pr_id=None: pytest.fail("must not resolve branch via gh_pr.get_pr")
    )
    captured = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.gh_pr.update",
        lambda gh, owner, repo, branch, title, description, screenshot, file, pr_id=None: captured.update(
            branch=branch, title=title, pr_id=pr_id
        ),
    )

    result = runner.invoke(app, ["pr", "update", "--pr-id", "99", "--title", "New title"])

    assert result.exit_code == 0, result.output
    assert captured == {"branch": "", "title": "New title", "pr_id": 99}


def test_pr_comment_with_pr_id_and_no_attachments_skips_branch_resolution(monkeypatch) -> None:
    """A message-only comment never uses `branch` (it only namespaces uploaded
    screenshots/files) -- with `--pr-id`, resolving it would cost an extra `gh pr view`
    round-trip for nothing, so it must be skipped entirely.
    """
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr(
        "bmsdna.devtools.cli.gh_pr.get_pr", lambda gh, pr_id=None: pytest.fail("must not resolve branch via gh_pr.get_pr")
    )
    captured = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.gh_pr.comment_with_screenshots",
        lambda gh, owner, repo, branch, message, screenshot, file, pr_id=None: captured.update(
            branch=branch, message=message, pr_id=pr_id
        ),
    )

    result = runner.invoke(app, ["pr", "comment", "--pr-id", "99", "--message", "hi"])

    assert result.exit_code == 0, result.output
    assert captured == {"branch": "", "message": "hi", "pr_id": 99}


def test_pr_comment_with_pr_id_and_a_screenshot_resolves_the_real_head_branch(monkeypatch, tmp_path) -> None:
    """With an attachment to namespace, `--pr-id` *does* need the PR's actual head branch --
    resolved via `gh_pr.get_pr`, not whatever's checked out locally (which may not even be
    this PR's branch).
    """
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.get_pr", lambda gh, pr_id=None: {"headRefName": "feature-y"})
    captured = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.gh_pr.comment_with_screenshots",
        lambda gh, owner, repo, branch, message, screenshot, file, pr_id=None: captured.update(branch=branch, pr_id=pr_id),
    )
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")

    result = runner.invoke(app, ["pr", "comment", "--pr-id", "99", "--screenshot", str(shot)])

    assert result.exit_code == 0, result.output
    assert captured == {"branch": "feature-y", "pr_id": 99}


# -- Azure DevOps -------------------------------------------------------------


def test_pr_status_with_pr_id_skips_branch_resolution_for_ado(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", _fail_current_branch)
    captured = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.run",
        lambda remote, pat, target_branch, wait, pr_id=None: captured.update(remote=remote, pr_id=pr_id),
    )

    result = runner.invoke(app, ["pr", "status", "--pr-id", "99"])

    assert result.exit_code == 0, result.output
    assert captured == {"remote": ADO_REMOTE, "pr_id": 99}


def test_pr_retry_with_pr_id_skips_branch_resolution_for_ado(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", _fail_current_branch)
    captured = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.retry",
        lambda remote, pat, target_branch, pr_id=None: captured.update(remote=remote, pr_id=pr_id),
    )

    result = runner.invoke(app, ["pr", "retry", "--pr-id", "99"])

    assert result.exit_code == 0, result.output
    assert captured == {"remote": ADO_REMOTE, "pr_id": 99}


def test_pr_publish_with_pr_id_resolves_by_id_and_skips_branch_for_ado(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", _fail_current_branch)
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.get_pr_by_id", lambda session, remote, pr_id: {"pullRequestId": pr_id}
    )
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.get_pr",
        lambda *a, **k: pytest.fail("must not resolve by branch when --pr-id is given"),
    )
    captured = {}
    monkeypatch.setattr("bmsdna.devtools.cli.pr_build.publish", lambda session, remote, pr: captured.update(pr=pr))

    result = runner.invoke(app, ["pr", "publish", "--pr-id", "99"])

    assert result.exit_code == 0, result.output
    assert captured == {"pr": {"pullRequestId": 99}}


def test_pr_update_with_pr_id_resolves_by_id_and_skips_branch_for_ado(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", _fail_current_branch)
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.get_pr_by_id", lambda session, remote, pr_id: {"pullRequestId": pr_id}
    )
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.get_pr",
        lambda *a, **k: pytest.fail("must not resolve by branch when --pr-id is given"),
    )
    captured = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.update",
        lambda session, remote, pr, title, description, screenshot, file: captured.update(pr=pr, title=title),
    )

    result = runner.invoke(app, ["pr", "update", "--pr-id", "99", "--title", "New title"])

    assert result.exit_code == 0, result.output
    assert captured == {"pr": {"pullRequestId": 99}, "title": "New title"}


def test_pr_comment_with_pr_id_resolves_by_id_and_skips_branch_for_ado(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", _fail_current_branch)
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.get_pr_by_id", lambda session, remote, pr_id: {"pullRequestId": pr_id}
    )
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.get_pr",
        lambda *a, **k: pytest.fail("must not resolve by branch when --pr-id is given"),
    )
    captured = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_build.comment_with_screenshots",
        lambda session, remote, pr_id, message, screenshot, file: captured.update(pr_id=pr_id, message=message),
    )

    result = runner.invoke(app, ["pr", "comment", "--pr-id", "99", "--message", "hi"])

    assert result.exit_code == 0, result.output
    assert captured == {"pr_id": 99, "message": "hi"}
