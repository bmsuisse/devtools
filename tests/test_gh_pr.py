from unittest.mock import MagicMock

import pytest

from bmsdna.devtools.gh_pr import (
    add_attachments,
    add_files,
    check_bucket,
    check_label,
    comment_with_screenshots,
    create,
    draft_notice,
    merge_conflict_message,
    protection_requires_status_checks,
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
