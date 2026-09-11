"""End-to-end request sequencing for ado_issue's create/update/delete flows,
against a fake `requests.Session` (no real network) so the call order, URLs,
and payloads sent to each Azure DevOps endpoint are verified together rather
than one function at a time.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bmsdna.devtools.ado_issue import (
    add_files,
    comment_with_screenshots,
    create,
    delete,
    delete_comment,
    search,
    update,
    update_comment,
)
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


def make_session(get_map: dict[str, dict] | None = None, wiql_ids: list[int] | None = None) -> MagicMock:
    session = MagicMock()
    get_map = get_map or {}

    def fake_get(url, params: dict | None = None, **kwargs):
        for fragment, body in get_map.items():
            if fragment in url:
                return FakeResponse(body)
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, params: dict | None = None, **kwargs):
        if "/_apis/wit/wiql" in url:
            return FakeResponse({"workItems": [{"id": i} for i in (wiql_ids or [])]})
        if "/_apis/wit/attachments" in url:
            assert params is not None
            assert "fileName" in params  # upload always names the attachment
            # Deliberately a *bare* url (no query string) here, to exercise
            # `upload_attachment`'s defensive fallback that appends `?fileName=...` itself
            # when the upload API's response doesn't already carry it.
            return FakeResponse({"id": "attach-1", "url": "https://dev.azure.com/myorg/_apis/wit/attachments/attach-1"})
        if url.endswith("/comments"):
            return FakeResponse({"id": 1, "text": kwargs["json"]["text"]})
        if "/_apis/wit/workitems/$" in url:
            return FakeResponse({"id": 123, "_links": {"html": {"href": "https://dev.azure.com/myorg/web/wi.aspx?id=123"}}})
        raise AssertionError(f"unexpected POST {url}")

    def fake_patch(url, params: dict | None = None, **kwargs):
        if "/comments/" in url:
            return FakeResponse({"id": int(url.rsplit("/", 1)[-1]), "text": kwargs["json"]["text"]})
        return FakeResponse({"id": 123, "rev": 2})

    def fake_delete(url, params: dict | None = None, **kwargs):
        return FakeResponse({}, status_code=204)

    session.get.side_effect = fake_get
    session.post.side_effect = fake_post
    session.patch.side_effect = fake_patch
    session.delete.side_effect = fake_delete
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
    assert relation["url"] == "https://dev.azure.com/myorg/_apis/wit/attachments/attach-1?fileName=00-shot.png"

    comment_text = session.post.call_args_list[2].kwargs["json"]["text"]
    # A raw HTML <img> tag, not Markdown `![]()` — the "Add Comment" REST API has no way to
    # request Markdown rendering (whether it's applied is an org-level rollout state outside
    # the caller's control), so `![]()` can end up showing as literal unrendered text. An
    # <img> tag renders either way. It must also carry `?fileName=...` in its src — without it
    # Azure DevOps serves the attachment as application/octet-stream with
    # Content-Disposition: attachment instead of e.g. image/png.
    assert '<img src="https://dev.azure.com/myorg/_apis/wit/attachments/attach-1?fileName=00-shot.png" alt="shot.png"' in comment_text


def test_create_with_files_uploads_links_and_comments(tmp_path) -> None:
    session = make_session()
    report = tmp_path / "report.pdf"
    report.write_bytes(b"fake-pdf-bytes")

    create(session, REMOTE, "Task", "Do the thing", "desc", None, ["tag1"], [], [str(report)])

    post_urls = [c.args[0] for c in session.post.call_args_list]
    assert post_urls[0] == "https://dev.azure.com/myorg/MyProj/_apis/wit/workitems/$Task"
    assert "/_apis/wit/attachments" in post_urls[1]
    assert post_urls[2] == "https://dev.azure.com/myorg/MyProj/_apis/wit/workItems/123/comments"

    comment_text = session.post.call_args_list[2].kwargs["json"]["text"]
    assert '<a href="https://dev.azure.com/myorg/_apis/wit/attachments/attach-1?fileName=00-report.pdf">report.pdf</a>' in comment_text
    assert "<img" not in comment_text


def test_create_with_screenshots_and_files_posts_a_single_combined_comment(tmp_path) -> None:
    session = make_session()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")
    report = tmp_path / "report.pdf"
    report.write_bytes(b"fake-pdf-bytes")

    create(session, REMOTE, "Task", "Do the thing", "desc", None, ["tag1"], [str(shot)], [str(report)])

    # Exactly one PATCH (linking both attachments together) and one comment POST — not two
    # separate round trips for the screenshot and the file.
    session.patch.assert_called_once()
    comment_posts = [c for c in session.post.call_args_list if c.args[0].endswith("/comments")]
    assert len(comment_posts) == 1
    comment_text = comment_posts[0].kwargs["json"]["text"]
    assert "<h2>Screenshots</h2>" in comment_text
    assert "<h2>Attachments</h2>" in comment_text


def test_add_files_links_attachment_and_comments(tmp_path) -> None:
    session = make_session()
    report = tmp_path / "report.pdf"
    report.write_bytes(b"fake-pdf-bytes")

    add_files(session, REMOTE, 123, [str(report)])

    session.patch.assert_called_once()
    comment_text = session.post.call_args_list[-1].kwargs["json"]["text"]
    assert '<a href="https://dev.azure.com/myorg/_apis/wit/attachments/attach-1?fileName=00-report.pdf">report.pdf</a>' in comment_text


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
    assert '<img src="https://dev.azure.com/myorg/_apis/wit/attachments/attach-1?fileName=00-after.png" alt="after.png"' in comment_text
    assert "Fixed" in comment_text


def test_comment_with_screenshots_and_files_links_both_and_sections_both(tmp_path) -> None:
    session = make_session()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")
    report = tmp_path / "report.pdf"
    report.write_bytes(b"fake-pdf-bytes")

    comment_with_screenshots(session, REMOTE, 42, "Fixed", [str(shot)], [str(report)])

    session.patch.assert_called_once()
    relations = session.patch.call_args.kwargs["json"]
    assert [r["value"]["attributes"]["comment"] for r in relations] == ["shot.png", "report.pdf"]

    comment_text = session.post.call_args_list[-1].kwargs["json"]["text"]
    assert "<h2>Screenshots</h2>" in comment_text
    assert "<h2>Attachments</h2>" in comment_text
    assert comment_text.index("<h2>Screenshots</h2>") < comment_text.index("<h2>Attachments</h2>")


def test_update_title_only_does_not_touch_board() -> None:
    session = make_session()

    update(session, REMOTE, 123, title="New title")

    session.get.assert_not_called()  # no --board given, so no team field values lookup at all
    patch_kwargs = session.patch.call_args.kwargs
    assert patch_kwargs["json"] == [{"op": "add", "path": "/fields/System.Title", "value": "New title"}]
    assert patch_kwargs["headers"]["Content-Type"] == "application/json-patch+json"


def test_update_with_board_resolves_area_path() -> None:
    session = make_session(get_map={"teamsettings/teamfieldvalues": {"defaultValue": "MyProj\\Other Team"}})

    update(session, REMOTE, 123, board="Other Team")

    get_url = session.get.call_args.args[0]
    assert "myorg/MyProj/Other%20Team/_apis/work/teamsettings/teamfieldvalues" in get_url
    ops = session.patch.call_args.kwargs["json"]
    assert ops == [{"op": "add", "path": "/fields/System.AreaPath", "value": "MyProj\\Other Team"}]


def test_delete_hits_work_item_delete_endpoint() -> None:
    session = make_session()

    delete(session, REMOTE, 123)

    session.delete.assert_called_once()
    delete_url, delete_kwargs = session.delete.call_args.args[0], session.delete.call_args.kwargs
    assert delete_url == "https://dev.azure.com/myorg/MyProj/_apis/wit/workitems/123"
    assert delete_kwargs["params"] == {"api-version": "7.1"}


def test_update_comment_hits_comment_id_endpoint() -> None:
    session = make_session()

    update_comment(session, REMOTE, 42, 7, "edited text")

    url, kwargs = session.patch.call_args.args[0], session.patch.call_args.kwargs
    assert url == "https://dev.azure.com/myorg/MyProj/_apis/wit/workItems/42/comments/7"
    assert kwargs["json"] == {"text": "edited text"}


def test_delete_comment_hits_comment_id_endpoint() -> None:
    session = make_session()

    delete_comment(session, REMOTE, 42, 7)

    url = session.delete.call_args.args[0]
    assert url == "https://dev.azure.com/myorg/MyProj/_apis/wit/workItems/42/comments/7"


def test_search_runs_wiql_then_batch_fetches_matched_fields() -> None:
    session = make_session(
        get_map={"/_apis/wit/workitems": {"value": [{"id": 42, "fields": {"System.Title": "Auth timeout bug", "System.State": "Active"}}]}},
        wiql_ids=[42],
    )

    results = search(session, REMOTE, ["auth", "timeout"])

    wiql_url, wiql_kwargs = session.post.call_args_list[0].args[0], session.post.call_args_list[0].kwargs
    assert wiql_url == "https://dev.azure.com/myorg/MyProj/_apis/wit/wiql"
    assert "auth" in wiql_kwargs["json"]["query"]
    assert "timeout" in wiql_kwargs["json"]["query"]

    get_url, get_kwargs = session.get.call_args.args[0], session.get.call_args.kwargs
    assert get_url == "https://dev.azure.com/myorg/MyProj/_apis/wit/workitems"
    assert get_kwargs["params"]["ids"] == "42"

    assert results == [{"id": 42, "fields": {"System.Title": "Auth timeout bug", "System.State": "Active"}}]


def test_search_preserves_wiql_order_when_batch_fetch_returns_a_different_order() -> None:
    # The batch "list work items by id" endpoint doesn't guarantee it echoes ids back in the
    # order they were requested — WIQL's ORDER BY [System.ChangedDate] DESC must still win.
    session = make_session(
        get_map={
            "/_apis/wit/workitems": {
                "value": [
                    {"id": 50, "fields": {"System.Title": "Oldest match", "System.State": "Active"}},
                    {"id": 102, "fields": {"System.Title": "Newest match", "System.State": "Active"}},
                    {"id": 99, "fields": {"System.Title": "Middle match", "System.State": "Active"}},
                ]
            }
        },
        wiql_ids=[102, 99, 50],
    )

    results = search(session, REMOTE, ["auth"])

    assert [item["id"] for item in results] == [102, 99, 50]


def test_search_skips_batch_fetch_when_no_matches() -> None:
    session = make_session(wiql_ids=[])

    results = search(session, REMOTE, ["nonexistent-keyword"])

    session.get.assert_not_called()
    assert results == []


def test_search_with_board_resolves_area_path_and_scopes_wiql() -> None:
    session = make_session(
        get_map={
            "teamsettings/teamfieldvalues": {"defaultValue": "MyProj\\Data Team"},
            "/_apis/wit/workitems": {"value": []},
        },
        wiql_ids=[],
    )

    search(session, REMOTE, ["auth"], board="Data Team")

    # Team field values lookup must happen before the WIQL query is built/run.
    get_url = session.get.call_args_list[0].args[0]
    assert "myorg/MyProj/Data%20Team/_apis/work/teamsettings/teamfieldvalues" in get_url

    wiql_kwargs = session.post.call_args.kwargs
    assert "[System.AreaPath] UNDER 'MyProj\\Data Team'" in wiql_kwargs["json"]["query"]


def test_search_without_board_does_not_look_up_area_path() -> None:
    session = make_session(wiql_ids=[])

    search(session, REMOTE, ["auth"])

    session.get.assert_not_called()  # no --board given, so no team field values lookup at all


def test_update_with_valid_state_includes_it_in_the_patch() -> None:
    session = make_session(
        get_map={
            "/_apis/wit/workitems/42": {"fields": {"System.WorkItemType": "Bug"}},
            "/_apis/wit/workitemtypes/Bug/states": {"value": [{"name": "New"}, {"name": "Active"}, {"name": "Closed"}]},
        }
    )

    update(session, REMOTE, 42, state="Closed")

    ops = session.patch.call_args.kwargs["json"]
    assert {"op": "add", "path": "/fields/System.State", "value": "Closed"} in ops


def test_update_state_uses_canonical_casing_from_valid_states() -> None:
    session = make_session(
        get_map={
            "/_apis/wit/workitems/42": {"fields": {"System.WorkItemType": "Bug"}},
            "/_apis/wit/workitemtypes/Bug/states": {"value": [{"name": "New"}, {"name": "Closed"}]},
        }
    )

    update(session, REMOTE, 42, state="closed")  # lowercase — doesn't match ADO's 'Closed' exactly

    ops = session.patch.call_args.kwargs["json"]
    assert {"op": "add", "path": "/fields/System.State", "value": "Closed"} in ops


def test_update_with_invalid_state_comments_instead_of_failing() -> None:
    session = make_session(
        get_map={
            "/_apis/wit/workitems/42": {"fields": {"System.WorkItemType": "Bug"}},
            "/_apis/wit/workitemtypes/Bug/states": {"value": [{"name": "New"}, {"name": "Active"}, {"name": "Closed"}]},
        }
    )

    result = update(session, REMOTE, 42, state="Done")

    assert result is None
    session.patch.assert_not_called()
    comment_url, comment_kwargs = session.post.call_args.args[0], session.post.call_args.kwargs
    assert comment_url == "https://dev.azure.com/myorg/MyProj/_apis/wit/workItems/42/comments"
    assert "'Done'" in comment_kwargs["json"]["text"]
    assert "Bug" in comment_kwargs["json"]["text"]


def test_update_with_invalid_state_still_patches_other_given_fields() -> None:
    session = make_session(
        get_map={
            "/_apis/wit/workitems/42": {"fields": {"System.WorkItemType": "Bug"}},
            "/_apis/wit/workitemtypes/Bug/states": {"value": [{"name": "New"}]},
        }
    )

    update(session, REMOTE, 42, title="New title", state="Done")

    ops = session.patch.call_args.kwargs["json"]
    assert ops == [{"op": "add", "path": "/fields/System.Title", "value": "New title"}]


def test_update_with_nothing_given_still_errors() -> None:
    session = make_session()

    with pytest.raises(SystemExit):
        update(session, REMOTE, 42)
