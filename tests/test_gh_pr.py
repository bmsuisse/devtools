import json
from unittest.mock import MagicMock

import pytest

from bmsdna.devtools.cli_tools import EXIT_NEEDS_APPROVAL
from bmsdna.devtools.gh_pr import (
    add_attachments,
    add_files,
    check_bucket,
    check_label,
    comment_with_screenshots,
    create,
    deploy_run_hint,
    draft_notice,
    failed_run_ids,
    get_workflow_runs_for_branch,
    latest_per_workflow,
    merge_conflict_message,
    protection_requires_status_checks,
    retry,
    retry_hint,
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
FAILED_CHECK_RUN = {
    "__typename": "CheckRun",
    "name": "build (ubuntu-latest)",
    "status": "COMPLETED",
    "conclusion": "FAILURE",
    "workflowName": "Python Test",
    "detailsUrl": "https://github.com/owner/repo/actions/runs/34882319228/job/104104355022",
}
FAILED_CHECK_RUN_SAME_RUN = {
    "__typename": "CheckRun",
    "name": "build (windows-latest)",
    "status": "COMPLETED",
    "conclusion": "FAILURE",
    "workflowName": "Python Test",
    "detailsUrl": "https://github.com/owner/repo/actions/runs/34882319228/job/104104399999",
}
FAILED_STATUS_CONTEXT = {"__typename": "StatusContext", "state": "FAILURE", "context": "external-ci"}


@pytest.mark.parametrize(
    "check,expected_bucket",
    [
        (COMPLETED_SUCCESS_CHECK_RUN, "pass"),
        (COMPLETED_SKIPPED_CHECK_RUN, "skipping"),
        ({"__typename": "CheckRun", "status": "IN_PROGRESS"}, "pending"),
        ({"__typename": "CheckRun", "status": "QUEUED"}, "pending"),
        ({"__typename": "CheckRun", "status": "WAITING"}, "waiting_approval"),
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


def test_run_wait_exits_1_not_2_when_a_check_already_failed_and_another_needs_approval(monkeypatch) -> None:
    """Regression: an already-failed check elsewhere in the PR must still end --wait even when
    another check is separately waiting on a deployment approval — must report the failure
    (exit 1), not silently prioritize the approval prompt (exit 2) or hang forever.
    """
    pr = {
        "number": 42,
        "title": "feat: widgets",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "isDraft": False,
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "deploy", "status": "WAITING", "workflowName": "Deploy"},
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "FAILURE", "workflowName": "CI"},
        ],
    }
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh: pr)

    with pytest.raises(SystemExit) as exc_info:
        run("gh", wait=True)

    assert exc_info.value.code == 1


def test_run_wait_does_not_hang_when_a_stuck_pending_check_also_exists(monkeypatch) -> None:
    """Regression: a genuinely-stuck pending check (e.g. downstream of the blocked deployment
    gate, so it can never leave "pending" on its own) combined with an already-failed check
    and a waiting-approval check must not send --wait into an infinite poll loop.
    """
    pr = {
        "number": 42,
        "title": "feat: widgets",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "isDraft": False,
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "deploy", "status": "WAITING", "workflowName": "Deploy"},
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "FAILURE", "workflowName": "CI"},
            {"__typename": "CheckRun", "name": "downstream", "status": "QUEUED", "workflowName": "CI"},
        ],
    }
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh: pr)
    monkeypatch.setattr("bmsdna.devtools.gh_pr.time.sleep", lambda s: pytest.fail("must not poll — would hang --wait forever"))

    with pytest.raises(SystemExit) as exc_info:
        run("gh", wait=True)

    assert exc_info.value.code == 1


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


def test_failed_run_ids_dedupes_and_ignores_non_failing_checks() -> None:
    checks = [COMPLETED_SUCCESS_CHECK_RUN, FAILED_CHECK_RUN, FAILED_CHECK_RUN_SAME_RUN]
    assert failed_run_ids(checks) == [34882319228]


def test_failed_run_ids_skips_status_context_with_no_details_url() -> None:
    assert failed_run_ids([FAILED_STATUS_CONTEXT]) == []


def test_failed_run_ids_empty_when_nothing_failed() -> None:
    assert failed_run_ids([COMPLETED_SUCCESS_CHECK_RUN, COMPLETED_SKIPPED_CHECK_RUN]) == []


def test_retry_hint_names_the_command(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert "bdt pr retry" in retry_hint()


def test_retry_hint_tells_claude_code_to_run_it(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    assert "bdt pr retry" in retry_hint()


def test_retry_reruns_each_distinct_failed_run(monkeypatch) -> None:
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        if "view" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps({"statusCheckRollup": [FAILED_CHECK_RUN, FAILED_CHECK_RUN_SAME_RUN]}), stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    retry("gh")

    rerun_cmds = [cmd for cmd in captured_cmds if "rerun" in cmd]
    assert len(rerun_cmds) == 1
    assert rerun_cmds[0] == ["gh", "run", "rerun", "34882319228", "--failed"]


def test_retry_exits_when_no_failed_run_found(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=0, stdout=json.dumps({"statusCheckRollup": [COMPLETED_SUCCESS_CHECK_RUN]}), stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    with pytest.raises(SystemExit):
        retry("gh")


def test_retry_exits_on_rerun_failure(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        if "view" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps({"statusCheckRollup": [FAILED_CHECK_RUN]}), stderr="")
        return MagicMock(returncode=1, stdout="", stderr="run is already in progress")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    with pytest.raises(SystemExit):
        retry("gh")


def test_retry_still_attempts_remaining_runs_after_one_fails(monkeypatch, capsys) -> None:
    """A failure reranning one run shouldn't stop bdt from attempting the others."""
    rerun_ids: list[str] = []

    def fake_run(cmd, **kwargs):
        if "view" in cmd:
            return MagicMock(
                returncode=0,
                stdout=json.dumps({"statusCheckRollup": [FAILED_CHECK_RUN, FAILED_CHECK_RUN_SAME_RUN]}),
                stderr="",
            )
        rerun_ids.append(cmd[3])
        if cmd[3] == "34882319228":
            return MagicMock(returncode=1, stdout="", stderr="run is already in progress")
        return MagicMock(returncode=0, stdout="", stderr="")

    # Make the two failed checks belong to *different* runs so both get a rerun attempt.
    other_run_check = {**FAILED_CHECK_RUN_SAME_RUN, "detailsUrl": "https://github.com/owner/repo/actions/runs/999/job/1"}
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.get_pr",
        lambda gh: {"statusCheckRollup": [FAILED_CHECK_RUN, other_run_check]},
    )
    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        retry("gh")

    assert rerun_ids == ["34882319228", "999"]
    assert "999" in capsys.readouterr().out
    assert "34882319228" in str(exc_info.value)


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


def test_run_watch_deploy_wait_stops_and_reports_pending_approval(monkeypatch, capsys) -> None:
    """--wait must not poll forever when the only deploy workflow run is paused on a
    deployment protection rule (status "waiting") -- it never completes on its own.
    """
    waiting_run = {**DEPLOY_RUN, "databaseId": 557, "status": "waiting", "conclusion": None}
    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", _fake_run_and_view([waiting_run]))

    with pytest.raises(SystemExit) as exc_info:
        run_watch_deploy("gh", "main", wait=True)

    assert exc_info.value.code == EXIT_NEEDS_APPROVAL
    assert "needs a reviewer" in capsys.readouterr().out


def test_run_watch_deploy_wait_exits_1_not_2_when_another_run_already_failed(monkeypatch) -> None:
    """A run stuck on approval must not mask an already-failed run in the same batch."""
    failed_run = {**DEPLOY_RUN, "databaseId": 556, "workflowName": "CI", "conclusion": "failure"}
    waiting_run = {**DEPLOY_RUN, "databaseId": 557, "workflowName": "Deploy", "status": "waiting", "conclusion": None}
    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", _fake_run_and_view([failed_run, waiting_run]))

    with pytest.raises(SystemExit) as exc_info:
        run_watch_deploy("gh", "main", wait=True)

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


def test_run_prints_deploy_hint_when_pr_has_no_checks_at_all(monkeypatch, capsys) -> None:
    """Regression: a PR with no `statusCheckRollup` entries at all (e.g. this repo's only
    workflow triggers on a push to the target branch, not on `pull_request`) is itself a
    settled state -- exactly when the hint is most useful -- so it must still be checked,
    not skipped just because there were no PR-triggered checks to report."""
    pr_view = {
        "number": 7,
        "title": "feat: x",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [],
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
    assert "no checks found" in out
    assert "bdt pr watch-deploy" in out


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
