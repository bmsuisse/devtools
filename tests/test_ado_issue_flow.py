"""End-to-end request sequencing for ado_issue.create()/comment_with_screenshots(),
against a fake `requests.Session` (no real network) so the call order, URLs,
and payloads sent to each Azure DevOps endpoint are verified together rather
than one function at a time.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from bmsdna.devtools.ado_issue import comment_with_screenshots, create
from bmsdna.devtools.gitrepo import AdoRemote

REMOTE = AdoRemote(org="myorg", project="MyProj", repo="myrepo")


class FakeResponse:
    def __init__(self, json_body: dict, status_code: int = 200):
        self._json = json_body
        self.status_code = status_code

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def make_session(get_map: dict[str, dict] | None = None) -> MagicMock:
    session = MagicMock()
    get_map = get_map or {}

    def fake_get(url, params: dict | None = None, **kwargs):
        for fragment, body in get_map.items():
            if fragment in url:
                return FakeResponse(body)
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, params: dict | None = None, **kwargs):
        if "/_apis/wit/attachments" in url:
            assert params is not None
            name = params["fileName"]
            return FakeResponse({"id": "attach-1", "url": f"https://dev.azure.com/myorg/_apis/wit/attachments/attach-1?fileName={name}"})
        if url.endswith("/comments"):
            return FakeResponse({"id": 1, "text": kwargs["json"]["text"]})
        if "/_apis/wit/workitems/$" in url:
            return FakeResponse({"id": 123, "_links": {"html": {"href": "https://dev.azure.com/myorg/web/wi.aspx?id=123"}}})
        raise AssertionError(f"unexpected POST {url}")

    def fake_patch(url, params=None, **kwargs):
        return FakeResponse({})

    session.get.side_effect = fake_get
    session.post.side_effect = fake_post
    session.patch.side_effect = fake_patch
    return session


def test_create_resolves_board_to_area_path_and_sets_it(tmp_path) -> None:
    session = make_session(get_map={"teamsettings/teamfieldvalues": {"defaultValue": "MyProj\\My Team"}})

    create(session, REMOTE, "Bug", "Widget is broken", None, "My Team", [], [])

    # First call must be the team field values lookup for the given board/team.
    get_url = session.get.call_args.args[0]
    assert "myorg/MyProj/My%20Team/_apis/work/teamsettings/teamfieldvalues" in get_url

    create_call = session.post.call_args_list[0]
    create_url, create_kwargs = create_call.args[0], create_call.kwargs
    assert create_url == "https://dev.azure.com/myorg/MyProj/_apis/wit/workitems/$Bug"
    ops = create_kwargs["json"]
    area_path_op = next(op for op in ops if op["path"] == "/fields/System.AreaPath")
    assert area_path_op["value"] == "MyProj\\My Team"
    assert create_kwargs["headers"]["Content-Type"] == "application/json-patch+json"


def test_create_with_screenshots_uploads_links_and_comments(tmp_path) -> None:
    session = make_session()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")

    create(session, REMOTE, "Task", "Do the thing", "desc", None, ["tag1"], [str(shot)])

    # POST calls in order: create work item, upload attachment, add comment.
    post_urls = [c.args[0] for c in session.post.call_args_list]
    assert post_urls[0] == "https://dev.azure.com/myorg/MyProj/_apis/wit/workitems/$Task"
    assert "/_apis/wit/attachments" in post_urls[1]
    assert post_urls[2] == "https://dev.azure.com/myorg/MyProj/_apis/wit/workItems/123/comments"

    # The attachment gets linked to work item 123 via a PATCH before the comment is posted.
    patch_url, patch_kwargs = session.patch.call_args.args[0], session.patch.call_args.kwargs
    assert patch_url == "https://dev.azure.com/myorg/MyProj/_apis/wit/workitems/123"
    relation = patch_kwargs["json"][0]["value"]
    assert relation["rel"] == "AttachedFile"
    assert "00-shot.png" in relation["url"]

    comment_text = session.post.call_args_list[2].kwargs["json"]["text"]
    assert "![shot.png]" in comment_text


def test_comment_with_screenshots_message_only() -> None:
    session = make_session()

    comment_with_screenshots(session, REMOTE, 42, "Looks good", [])

    assert session.post.call_count == 1
    url, kwargs = session.post.call_args.args[0], session.post.call_args.kwargs
    assert url == "https://dev.azure.com/myorg/MyProj/_apis/wit/workItems/42/comments"
    assert kwargs["json"] == {"text": "Looks good"}
    session.patch.assert_not_called()


def test_comment_with_screenshots_links_attachments_before_commenting(tmp_path) -> None:
    session = make_session()
    shot = tmp_path / "after.png"
    shot.write_bytes(b"fake-png-bytes")

    comment_with_screenshots(session, REMOTE, 42, "Fixed", [str(shot)])

    session.patch.assert_called_once()
    comment_text = session.post.call_args_list[-1].kwargs["json"]["text"]
    assert "![after.png]" in comment_text
    assert "Fixed" in comment_text
