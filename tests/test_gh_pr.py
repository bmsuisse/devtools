import json
from unittest.mock import MagicMock

import pytest

from bmsdna.devtools.gh_pr import (
    add_attachments,
    add_files,
    check_bucket,
    check_label,
    comment_with_screenshots,
    create,
    deploy_run_hint,
    draft_notice,
    get_workflow_runs_for_branch,
    latest_per_workflow,
    merge_conflict_message,
    protection_requires_status_checks,
    run,
    run_watch_deploy,
    update,
)

# Real statusCheckRollup entries captured from `gh pr view 13902 -R cli/cli --json statusCheckRollup`.
COMPLETED_SUCCESS_CHECK_RUN = {
    "__typename": "CheckRun",
    "name": "label-external / label_issues",
    "status": "COMPLETED",
    "conclusion": "SUCCESS",
    "workflowName": "PR Triaging",
}
COMPLETED_SKIPPED_CHECK_RUN = {
    "__typename": "CheckRun",
    "name": "close-from-default-branch / close-from-default-branch",
    "status": "COMPLETED",
    "conclusion": "SKIPPED",
    "workflowName": "PR Triaging",
}


@pytest.mark.parametrize(
    "check,expected_bucket",
    [
        (COMPLETED_SUCCESS_CHECK_RUN, "pass"),
        (COMPLETED_SKIPPED_CHECK_RUN, "skipping"),
        ({"__typename": "CheckRun", "status": "IN_PROGRESS"}, "pending"),
        ({"__typename": "CheckRun", "status": "QUEUED"}, "pending"),
        ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}, "fail"),
        ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "TIMED_OUT"}, "fail"),
        ({"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"}, "cancel"),
        ({"__typename": "StatusContext", "state": "SUCCESS"}, "pass"),
        ({"__typename": "StatusContext", "state": "PENDING"}, "pending"),
        ({"__typename": "StatusContext", "state": "ERROR"}, "fail"),
        ({"__typename": "StatusContext", "state": "FAILURE"}, "fail"),
    ],
)
def test_check_bucket(check: dict, expected_bucket: str) -> None:
    assert check_bucket(check) == expected_bucket


def test_check_label_prefixes_workflow_when_distinct() -> None:
    assert check_label(COMPLETED_SUCCESS_CHECK_RUN) == "PR Triaging / label-external / label_issues"


def test_check_label_no_duplicate_when_workflow_name_already_in_name() -> None:
    check = {"name": "build (ubuntu-latest)", "workflowName": "build (ubuntu-latest)"}
    assert check_label(check) == "build (ubuntu-latest)"


@pytest.mark.parametrize("mergeable", ["MERGEABLE", "UNKNOWN", None])
def test_merge_conflict_message_none_when_not_conflicting(mergeable: str | None) -> None:
    pr = {"number": 1, "title": "x", "baseRefName": "main", "mergeable": mergeable}
    assert merge_conflict_message(pr) is None


def test_merge_conflict_message_conflicting() -> None:
    pr = {"number": 42, "title": "feat: widgets", "baseRefName": "main", "mergeable": "CONFLICTING"}
    msg = merge_conflict_message(pr)
    assert msg is not None
    assert "PR #42" in msg
    assert "main" in msg


def test_draft_notice_none_when_not_draft(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    pr = {"number": 1, "title": "x", "isDraft": False}
    assert draft_notice(pr) is None


def test_draft_notice_when_draft(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    pr = {"number": 42, "title": "feat: widgets", "isDraft": True}
    msg = draft_notice(pr)
    assert msg is not None
    assert "PR #42" in msg
    assert "bdt pr publish" in msg
    assert "/code-review" not in msg


def test_draft_notice_tells_claude_code_to_review_first(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    pr = {"number": 42, "title": "feat: widgets", "isDraft": True}
    msg = draft_notice(pr)
    assert msg is not None
    assert "PR #42" in msg
    assert "/code-review" in msg
    assert "bdt pr publish" in msg


def test_protection_requires_status_checks_true_when_configured() -> None:
    protection = {"required_status_checks": {"strict": True, "contexts": ["build"]}}
    assert protection_requires_status_checks(protection) is True


@pytest.mark.parametrize("protection", [{}, {"required_status_checks": None}])
def test_protection_requires_status_checks_false_when_absent(protection: dict) -> None:
    assert protection_requires_status_checks(protection) is False


def test_create_passes_repeatable_label_flags(monkeypatch) -> None:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    returncode, url = create("gh", "main", [], labels=["bug", "urgent"])

    assert returncode == 0
    assert url == "https://github.com/owner/repo/pull/7"
    assert captured_cmd.count("--label") == 2
    assert captured_cmd[captured_cmd.index("--label") + 1] == "bug"


def test_create_returns_none_url_on_failure(monkeypatch, capsys) -> None:
    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=1, stdout="", stderr="label 'nope' not found")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    returncode, url = create("gh", "main", [], labels=["nope"])

    assert returncode == 1
    assert url is None
    assert "not found" in capsys.readouterr().err


def test_create_without_agent_makes_no_follow_up_calls(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    create("gh", "main", [])

    assert len(calls) == 1


def test_create_appends_agent_session_note_when_detected(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    edited_body: list[str] = []

    def fake_run(cmd, **kwargs):
        if "view" in cmd:
            return MagicMock(returncode=0, stdout='{"number": 7, "title": "Fix bug", "body": "PR body"}', stderr="")
        if "edit" in cmd:
            edited_body.append(cmd[cmd.index("--body") + 1])
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    create("gh", "main", [])

    assert edited_body == ["PR body\n\nClaude Session: https://claude.ai/code/session_abc123"]


def _fake_push_assets(monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.push_assets",
        lambda owner, repo, branch, paths, **kwargs: [f"https://github.com/{owner}/{repo}/blob/pr-assets/{branch}/{i:02d}-{p.split('/')[-1]}?raw=true" for i, p in enumerate(paths)],
    )


def test_add_files_pushes_and_appends_attachments_section(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        if "view" in cmd:
            return MagicMock(returncode=0, stdout='{"number": 7, "body": "existing body"}', stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    add_files("gh", "owner", "repo", "feature-x", ["/tmp/report.pdf"])

    edit_cmd = next(cmd for cmd in captured_cmds if "edit" in cmd)
    body = edit_cmd[edit_cmd.index("--body") + 1]
    assert "## Attachments" in body
    assert "[report.pdf]" in body
    assert "existing body" in body


def test_add_attachments_edits_pr_body_once_for_both_kinds(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        if "view" in cmd:
            return MagicMock(returncode=0, stdout='{"number": 7, "body": "existing body"}', stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    add_attachments("gh", "owner", "repo", "feature-x", screenshot_paths=["/tmp/shot.png"], file_paths=["/tmp/report.pdf"])

    view_cmds = [cmd for cmd in captured_cmds if "view" in cmd]
    edit_cmds = [cmd for cmd in captured_cmds if "edit" in cmd]
    assert len(view_cmds) == 1
    assert len(edit_cmds) == 1
    body = edit_cmds[0][edit_cmds[0].index("--body") + 1]
    assert "## Screenshots" in body
    assert "## Attachments" in body
    assert body.index("## Screenshots") < body.index("## Attachments")


def test_update_appends_both_screenshots_and_files(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        if "view" in cmd:
            return MagicMock(returncode=0, stdout='{"number": 7, "body": "existing body"}', stderr="")
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    update("gh", "owner", "repo", "feature-x", screenshot_paths=["/tmp/shot.png"], file_paths=["/tmp/report.pdf"])

    body = captured_cmd[captured_cmd.index("--body") + 1]
    assert "## Screenshots" in body
    assert "## Attachments" in body
    assert body.index("## Screenshots") < body.index("## Attachments")


def test_update_skips_session_note_if_already_in_current_title(monkeypatch) -> None:
    """`update()` fetches the PR's title (not just its body) so an agent session already
    referenced there -- not just in the body being replaced -- is still recognized."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        if "view" in cmd:
            return MagicMock(
                returncode=0,
                stdout='{"number": 7, "title": "Fix bug (Claude Session: https://claude.ai/code/session_abc123)", "body": "old body"}',
                stderr="",
            )
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    update("gh", "owner", "repo", "feature-x", description="new body")

    body = captured_cmd[captured_cmd.index("--body") + 1]
    assert body == "new body"


def test_comment_with_screenshots_and_files_builds_both_sections(monkeypatch) -> None:
    _fake_push_assets(monkeypatch)
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    comment_with_screenshots("gh", "owner", "repo", "feature-x", "Fixed", ["/tmp/shot.png"], ["/tmp/report.pdf"])

    body = captured_cmd[captured_cmd.index("--body") + 1]
    assert "Fixed" in body
    assert "## Screenshots" in body
    assert "## Attachments" in body


def test_comment_with_screenshots_appends_agent_session_note_when_detected(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    comment_with_screenshots("gh", "owner", "repo", "feature-x", "Fixed", [], [])

    body = captured_cmd[captured_cmd.index("--body") + 1]
    assert body == "Fixed\n\nClaude Session: https://claude.ai/code/session_abc123"


# A workflow run triggered by a push to a branch (as opposed to a `pull_request` trigger),
# shaped like `gh run list --branch main --event push --json ...` actually returns.
DEPLOY_RUN = {
    "databaseId": 555,
    "name": "Deploy",
    "workflowName": "Deploy",
    "status": "completed",
    "conclusion": "success",
    "url": "https://github.com/owner/repo/actions/runs/555",
    "headBranch": "main",
}


def test_get_workflow_runs_for_branch_filters_by_branch_and_push_event(monkeypatch) -> None:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps([DEPLOY_RUN]), stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    runs = get_workflow_runs_for_branch("gh", "main", limit=3)

    assert runs == [DEPLOY_RUN]
    assert captured_cmd[captured_cmd.index("--branch") + 1] == "main"
    assert captured_cmd[captured_cmd.index("--event") + 1] == "push"
    assert captured_cmd[captured_cmd.index("--limit") + 1] == "3"


def test_get_workflow_runs_for_branch_exits_on_gh_failure(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=1, stdout="", stderr="gh: not logged in")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    with pytest.raises(SystemExit):
        get_workflow_runs_for_branch("gh", "main")


def test_latest_per_workflow_keeps_highest_id_per_workflow() -> None:
    runs = [
        {"databaseId": 1, "workflowName": "CI"},
        {"databaseId": 3, "workflowName": "CI"},
        {"databaseId": 2, "workflowName": "Deploy"},
    ]

    result = latest_per_workflow(runs)

    assert {r["databaseId"] for r in result} == {3, 2}


def test_deploy_run_hint_none_when_no_runs_on_target(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_workflow_runs_for_branch", lambda gh, branch, limit=5: [])
    assert deploy_run_hint("gh", "main") is None


def test_deploy_run_hint_mentions_branch_and_run_when_found(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_workflow_runs_for_branch", lambda gh, branch, limit=5: [DEPLOY_RUN])

    hint = deploy_run_hint("gh", "main")

    assert hint is not None
    assert "main" in hint
    assert "#555" in hint
    assert "bdt pr watch-deploy" in hint


def test_deploy_run_hint_fails_open_on_error(monkeypatch) -> None:
    def raise_exit(gh, branch, limit=5):
        raise SystemExit("boom")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_workflow_runs_for_branch", raise_exit)

    assert deploy_run_hint("gh", "main") is None


def _fake_run_and_view(runs_json: list[dict], log_failed_output: str = ""):
    def fake_run(cmd, **kwargs):
        if "list" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps(runs_json), stderr="")
        if "view" in cmd:
            return MagicMock(returncode=0, stdout=log_failed_output, stderr="")
        raise AssertionError(f"unexpected command {cmd}")

    return fake_run


def test_run_watch_deploy_prints_completed_run_and_returns_without_wait(monkeypatch, capsys) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", _fake_run_and_view([DEPLOY_RUN]))

    run_watch_deploy("gh", "main", wait=False)

    out = capsys.readouterr().out
    assert "Deploy #555" in out
    assert "SUCCESS" in out


def test_run_watch_deploy_no_runs_prints_message_and_returns(monkeypatch, capsys) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", _fake_run_and_view([]))

    run_watch_deploy("gh", "main", wait=False)

    assert "No workflow runs found on 'main'" in capsys.readouterr().out


def test_run_watch_deploy_exits_1_on_failed_run(monkeypatch) -> None:
    failed_run = {**DEPLOY_RUN, "databaseId": 556, "conclusion": "failure"}
    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", _fake_run_and_view([failed_run]))

    with pytest.raises(SystemExit) as exc_info:
        run_watch_deploy("gh", "main", wait=False)

    assert exc_info.value.code == 1


def test_run_prints_deploy_hint_after_checks_pass(monkeypatch, capsys) -> None:
    pr_view = {
        "number": 7,
        "title": "feat: x",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [COMPLETED_SUCCESS_CHECK_RUN],
        "isDraft": False,
    }

    def fake_run(cmd, **kwargs):
        if "pr" in cmd and "view" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps(pr_view), stderr="")
        if "run" in cmd and "list" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps([DEPLOY_RUN]), stderr="")
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    run("gh", wait=False)

    out = capsys.readouterr().out
    assert "bdt pr watch-deploy" in out
    assert "main" in out


def test_run_skips_deploy_hint_when_checks_still_pending_without_wait(monkeypatch, capsys) -> None:
    """Regression: without --wait, a still-pending check must not be mistaken for
    'checks succeeded' -- the hint should only ever follow a genuinely settled result."""
    pr_view = {
        "number": 7,
        "title": "feat: x",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"__typename": "CheckRun", "status": "IN_PROGRESS", "name": "build"}],
        "isDraft": False,
    }
    hint_calls: list[str] = []

    def fake_run(cmd, **kwargs):
        if "pr" in cmd and "view" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps(pr_view), stderr="")
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.deploy_run_hint",
        lambda gh, branch: hint_calls.append(branch) or "should not print",
    )

    run("gh", wait=False)

    assert hint_calls == []
    assert "should not print" not in capsys.readouterr().out


def test_run_skips_deploy_hint_when_check_failed(monkeypatch, capsys) -> None:
    pr_view = {
        "number": 7,
        "title": "feat: x",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE", "name": "build"}],
        "isDraft": False,
    }
    hint_calls: list[str] = []

    def fake_run(cmd, **kwargs):
        if "pr" in cmd and "view" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps(pr_view), stderr="")
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.deploy_run_hint",
        lambda gh, branch: hint_calls.append(branch) or "should not print",
    )

    with pytest.raises(SystemExit):
        run("gh", wait=False)

    assert hint_calls == []
    assert "should not print" not in capsys.readouterr().out
