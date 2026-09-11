"""Request-level coverage for pr_build's screenshot/file attach paths, against a fake
`requests.Session` (no real network).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from bmsdna.devtools.gitrepo import AdoRemote
from bmsdna.devtools.pr_build import add_attachments, add_files, add_screenshots, comment_with_screenshots, update

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


def test_add_screenshots_still_works_unaffected(tmp_path) -> None:
    session = make_session()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")

    add_screenshots(session, REMOTE, PR, [str(shot)])

    description = session.patch.call_args.kwargs["json"]["description"]
    assert "## Screenshots" in description
    assert "![shot.png]" in description
