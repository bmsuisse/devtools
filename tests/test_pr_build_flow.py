"""Request-level coverage for pr_build's screenshot/file attach paths, against a fake
`requests.Session` (no real network).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from bmsdna.devtools.gitrepo import AdoRemote
from bmsdna.devtools.pr_build import (
    add_attachments,
    add_files,
    add_screenshots,
    comment_with_screenshots,
    deploy_build_hint,
    ensure_session_note,
    get_builds_for_branch,
    run,
    run_watch_deploy,
    update,
)

REMOTE = AdoRemote(org="myorg", project="MyProj", repo="myrepo")
PR = {"pullRequestId": 42, "title": "feat: widgets", "description": "existing description"}


class FakeResponse:
    def __init__(self, json_body: dict, status_code: int = 200):
        self._json = json_body
        self.status_code = status_code

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def make_session() -> MagicMock:
    session = MagicMock()

    def fake_post(url, params=None, **kwargs):
        if "/attachments/" in url:
            return FakeResponse({"url": f"https://dev.azure.com/myorg/_apis/git/repositories/myrepo/pullRequests/42/attachments/{url.rsplit('/', 1)[-1]}"})
        if url.endswith("/threads"):
            return FakeResponse({"id": 1})
        raise AssertionError(f"unexpected POST {url}")

    def fake_patch(url, params=None, **kwargs):
        return FakeResponse({"pullRequestId": 42})

    session.post.side_effect = fake_post
    session.patch.side_effect = fake_patch
    return session


def test_add_files_appends_attachments_section_with_link(tmp_path) -> None:
    session = make_session()
    report = tmp_path / "report.pdf"
    report.write_bytes(b"fake-pdf-bytes")

    add_files(session, REMOTE, PR, [str(report)])

    patch_kwargs = session.patch.call_args.kwargs
    description = patch_kwargs["json"]["description"]
    assert "## Attachments" in description
    assert "[report.pdf]" in description
    assert "existing description" in description


def test_update_appends_both_screenshots_and_files(tmp_path) -> None:
    session = make_session()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")
    report = tmp_path / "report.pdf"
    report.write_bytes(b"fake-pdf-bytes")

    update(session, REMOTE, PR, screenshot_paths=[str(shot)], file_paths=[str(report)])

    description = session.patch.call_args.kwargs["json"]["description"]
    assert "## Screenshots" in description
    assert "## Attachments" in description
    assert description.index("## Screenshots") < description.index("## Attachments")


def test_comment_with_screenshots_and_files_builds_both_sections(tmp_path) -> None:
    session = make_session()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")
    report = tmp_path / "report.pdf"
    report.write_bytes(b"fake-pdf-bytes")

    comment_with_screenshots(session, REMOTE, 42, "Fixed", [str(shot)], [str(report)])

    content = session.post.call_args.kwargs["json"]["comments"][0]["content"]
    assert "Fixed" in content
    assert "## Screenshots" in content
    assert "## Attachments" in content


def test_add_attachments_patches_pr_description_once_for_both_kinds(tmp_path) -> None:
    session = make_session()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")
    report = tmp_path / "report.pdf"
    report.write_bytes(b"fake-pdf-bytes")

    add_attachments(session, REMOTE, PR, screenshot_paths=[str(shot)], file_paths=[str(report)])

    session.patch.assert_called_once()
    description = session.patch.call_args.kwargs["json"]["description"]
    assert "## Screenshots" in description
    assert "## Attachments" in description
    assert description.index("## Screenshots") < description.index("## Attachments")


def test_add_attachments_returns_pr_with_updated_description(tmp_path) -> None:
    session = make_session()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")

    updated = add_attachments(session, REMOTE, PR, screenshot_paths=[str(shot)])

    assert updated["pullRequestId"] == PR["pullRequestId"]
    assert updated["description"] == session.patch.call_args.kwargs["json"]["description"]
    assert "## Screenshots" in updated["description"]


def test_ensure_session_note_appends_when_agent_detected(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    session = make_session()

    ensure_session_note(session, REMOTE, PR)

    description = session.patch.call_args.kwargs["json"]["description"]
    assert description == "existing description\n\nClaude Session: https://claude.ai/code/session_abc123"


def test_ensure_session_note_is_a_noop_without_agent() -> None:
    session = make_session()

    ensure_session_note(session, REMOTE, PR)

    session.patch.assert_not_called()


def test_add_screenshots_still_works_unaffected(tmp_path) -> None:
    session = make_session()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")

    add_screenshots(session, REMOTE, PR, [str(shot)])

    description = session.patch.call_args.kwargs["json"]["description"]
    assert "## Screenshots" in description
    assert "![shot.png]" in description


def make_builds_session(builds: list[dict]) -> MagicMock:
    session = MagicMock()

    def fake_get(url, params=None, **kwargs):
        if url.endswith("/_apis/build/builds"):
            return FakeResponse({"value": builds})
        if "/timeline" in url:
            return FakeResponse({"records": []})
        raise AssertionError(f"unexpected GET {url}")

    session.get.side_effect = fake_get
    return session


DEPLOY_BUILD = {
    "id": 99,
    "definition": {"id": 1, "name": "Deploy"},
    "status": "completed",
    "result": "succeeded",
    "buildNumber": "99",
    "startTime": "2024-01-01T00:00:00Z",
    "finishTime": "2024-01-01T00:05:00Z",
    "sourceVersion": "abcdef1234567890",
}


def test_get_builds_for_branch_queries_target_branch_ref() -> None:
    session = make_builds_session([DEPLOY_BUILD])

    builds = get_builds_for_branch(session, REMOTE, "main", top=3)

    assert builds == [DEPLOY_BUILD]
    params = session.get.call_args.kwargs["params"]
    assert params["branchName"] == "refs/heads/main"
    assert params["$top"] == 3


def test_deploy_build_hint_none_when_no_builds_on_target() -> None:
    session = make_builds_session([])
    assert deploy_build_hint(session, REMOTE, "main") is None


def test_deploy_build_hint_mentions_branch_and_build_when_found() -> None:
    session = make_builds_session([DEPLOY_BUILD])

    hint = deploy_build_hint(session, REMOTE, "main")

    assert hint is not None
    assert "main" in hint
    assert "#99" in hint
    assert "bdt pr watch-deploy" in hint


def test_deploy_build_hint_fails_open_on_request_error() -> None:
    session = MagicMock()
    session.get.side_effect = requests.RequestException("boom")
    assert deploy_build_hint(session, REMOTE, "main") is None


def test_run_watch_deploy_prints_completed_build_and_returns_without_wait(monkeypatch, capsys) -> None:
    session = make_builds_session([DEPLOY_BUILD])
    monkeypatch.setattr("bmsdna.devtools.pr_build.requests.Session", lambda: session)

    run_watch_deploy(REMOTE, "fake-pat", "main", wait=False)

    out = capsys.readouterr().out
    assert "Deploy #99" in out
    assert "SUCCEEDED" in out


def test_run_watch_deploy_no_builds_prints_message_and_returns(monkeypatch, capsys) -> None:
    session = make_builds_session([])
    monkeypatch.setattr("bmsdna.devtools.pr_build.requests.Session", lambda: session)

    run_watch_deploy(REMOTE, "fake-pat", "main", wait=False)

    out = capsys.readouterr().out
    assert "No builds found on 'main'" in out


def test_run_watch_deploy_exits_1_on_failed_build(monkeypatch) -> None:
    failed_build = {**DEPLOY_BUILD, "id": 100, "result": "failed"}
    session = make_builds_session([failed_build])
    monkeypatch.setattr("bmsdna.devtools.pr_build.requests.Session", lambda: session)

    with pytest.raises(SystemExit) as exc_info:
        run_watch_deploy(REMOTE, "fake-pat", "main", wait=False)

    assert exc_info.value.code == 1


def test_run_prints_deploy_hint_after_reporting_pr_success(monkeypatch, capsys) -> None:
    pr = {"pullRequestId": 1, "title": "feat: x", "status": "active", "isDraft": False}
    ci_build = {**DEPLOY_BUILD, "id": 5, "definition": {"id": 2, "name": "CI"}}

    monkeypatch.setattr("bmsdna.devtools.pr_build.requests.Session", lambda: MagicMock())
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda session, remote, source, target: pr)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_builds_for_pr", lambda session, remote, source, pr_id: [ci_build])
    monkeypatch.setattr("bmsdna.devtools.pr_build.deploy_build_hint", lambda session, remote, target: "HINT: run `bdt pr watch-deploy`")

    run(REMOTE, None, "main", wait=False, source_branch="feature-x")

    assert "HINT: run `bdt pr watch-deploy`" in capsys.readouterr().out


def test_run_skips_deploy_hint_when_pr_build_failed(monkeypatch, capsys) -> None:
    pr = {"pullRequestId": 1, "title": "feat: x", "status": "active", "isDraft": False}
    ci_build = {**DEPLOY_BUILD, "id": 5, "definition": {"id": 2, "name": "CI"}, "result": "failed"}
    hint_calls: list[str] = []

    monkeypatch.setattr("bmsdna.devtools.pr_build.requests.Session", lambda: make_builds_session([]))
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda session, remote, source, target: pr)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_builds_for_pr", lambda session, remote, source, pr_id: [ci_build])

    def fake_hint(session, remote, target):
        hint_calls.append(target)
        return "should not print"

    monkeypatch.setattr("bmsdna.devtools.pr_build.deploy_build_hint", fake_hint)

    with pytest.raises(SystemExit):
        run(REMOTE, None, "main", wait=False, source_branch="feature-x")

    assert hint_calls == []
    assert "should not print" not in capsys.readouterr().out
