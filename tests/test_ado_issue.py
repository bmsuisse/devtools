from unittest.mock import MagicMock

from bmsdna.devtools.ado_issue import (
    build_attach_ops,
    build_create_ops,
    build_search_wiql,
    build_update_ops,
    edit_url,
    html_url,
    resolve_board,
    upload_attachment,
)
from bmsdna.devtools.gitrepo import AdoRemote

REMOTE = AdoRemote(org="myorg", project="MyProj", repo="myrepo")


def write_pyproject(tmp_path, body: str):
    (tmp_path / "pyproject.toml").write_text(body)
    return tmp_path


def test_resolve_board_param_wins_over_pyproject(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.bdt.ado]\nboard = 'From Pyproject'\n")
    assert resolve_board("From Param", tmp_path) == "From Param"


def test_resolve_board_falls_back_to_pyproject(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.bdt.ado]\nboard = 'From Pyproject'\n")
    assert resolve_board(None, tmp_path) == "From Pyproject"


def test_resolve_board_none_when_neither_set(tmp_path) -> None:
    assert resolve_board(None, tmp_path) is None


def test_resolve_board_none_when_no_pyproject(tmp_path) -> None:
    assert resolve_board(None, tmp_path / "nonexistent") is None


def test_build_create_ops_title_only() -> None:
    assert build_create_ops("Sample task") == [{"op": "add", "path": "/fields/System.Title", "value": "Sample task"}]


def test_build_create_ops_includes_optional_fields() -> None:
    ops = build_create_ops("Title", description="Body", area_path="Proj\\Team", tags=["a", "b"])
    paths = {op["path"]: op["value"] for op in ops}
    assert paths["/fields/System.Title"] == "Title"
    assert paths["/fields/System.Description"] == "Body"
    assert paths["/fields/System.AreaPath"] == "Proj\\Team"
    assert paths["/fields/System.Tags"] == "a; b"


def test_build_create_ops_omits_empty_optional_fields() -> None:
    ops = build_create_ops("Title", description=None, area_path=None, tags=None)
    assert [op["path"] for op in ops] == ["/fields/System.Title"]


def test_build_create_ops_opts_description_into_markdown_formatting() -> None:
    # Description defaults to HTML formatting via the REST API — without this op, Markdown
    # syntax passed to --description (##, **bold**, `code`, - lists) renders as literal text.
    ops = build_create_ops("Title", description="## Heading\n\n- one\n- two")
    assert {"op": "add", "path": "/multilineFieldsFormat/System.Description", "value": "Markdown"} in ops


def test_build_create_ops_no_markdown_format_op_without_description() -> None:
    ops = build_create_ops("Title")
    assert not any(op["path"] == "/multilineFieldsFormat/System.Description" for op in ops)


def test_build_attach_ops_shape() -> None:
    ops = build_attach_ops([("shot.png", "https://dev.azure.com/x/_apis/wit/attachments/1?fileName=shot.png")])
    assert ops == [
        {
            "op": "add",
            "path": "/relations/-",
            "value": {
                "rel": "AttachedFile",
                "url": "https://dev.azure.com/x/_apis/wit/attachments/1?fileName=shot.png",
                "attributes": {"comment": "shot.png"},
            },
        }
    ]


def test_upload_attachment_appends_filename_query_if_missing(tmp_path) -> None:
    """Azure DevOps' WIT attachment-download endpoint only infers Content-Type/
    Content-Disposition from a `?fileName=...` query param — without it, a GET serves
    `application/octet-stream` with `Content-Disposition: attachment` instead of e.g.
    `image/png` (confirmed against a live org), which would make a Markdown `![]()` embed show
    a broken image instead of the picture. `upload_attachment` guards against an upload-API
    response whose `url` doesn't already carry it.
    """
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")
    session = MagicMock()
    session.post.return_value.json.return_value = {
        "id": "abc-123",
        "url": "https://dev.azure.com/myorg/MyProj/_apis/wit/attachments/abc-123",
    }

    attachment_id, url = upload_attachment(session, REMOTE, "shot.png", str(shot))

    assert attachment_id == "abc-123"
    assert url == "https://dev.azure.com/myorg/MyProj/_apis/wit/attachments/abc-123?fileName=shot.png"


def test_upload_attachment_url_encodes_filename_with_special_characters(tmp_path) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")
    session = MagicMock()
    session.post.return_value.json.return_value = {
        "id": "abc-123",
        "url": "https://dev.azure.com/myorg/MyProj/_apis/wit/attachments/abc-123",
    }

    _, url = upload_attachment(session, REMOTE, "00-my screenshot.png", str(shot))

    assert url.endswith("?fileName=00-my%20screenshot.png")


def test_upload_attachment_does_not_double_append_when_already_present(tmp_path) -> None:
    # The real API echoes the fileName query param it was given back in `url` — must not
    # append a second one on top (that would produce a malformed `?fileName=x?fileName=x`).
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png-bytes")
    session = MagicMock()
    session.post.return_value.json.return_value = {
        "id": "abc-123",
        "url": "https://dev.azure.com/myorg/MyProj/_apis/wit/attachments/abc-123?fileName=shot.png",
    }

    _, url = upload_attachment(session, REMOTE, "shot.png", str(shot))

    assert url == "https://dev.azure.com/myorg/MyProj/_apis/wit/attachments/abc-123?fileName=shot.png"


def test_build_update_ops_empty_when_nothing_given() -> None:
    assert build_update_ops() == []


def test_build_update_ops_only_touches_given_fields() -> None:
    ops = build_update_ops(title="New title")
    assert ops == [{"op": "add", "path": "/fields/System.Title", "value": "New title"}]


def test_build_update_ops_all_fields() -> None:
    ops = build_update_ops(title="T", description="D", area_path="Proj\\Team", tags=["a", "b"], state="Resolved")
    paths = {op["path"]: op["value"] for op in ops}
    assert paths == {
        "/fields/System.Title": "T",
        "/fields/System.Description": "D",
        "/multilineFieldsFormat/System.Description": "Markdown",
        "/fields/System.AreaPath": "Proj\\Team",
        "/fields/System.Tags": "a; b",
        "/fields/System.State": "Resolved",
    }


def test_build_update_ops_empty_tags_list_clears_tags() -> None:
    # An explicit [] (distinct from the default None) is a deliberate "clear all tags".
    ops = build_update_ops(tags=[])
    assert ops == [{"op": "add", "path": "/fields/System.Tags", "value": ""}]


def test_build_update_ops_opts_description_into_markdown_formatting() -> None:
    ops = build_update_ops(description="## Heading")
    assert {"op": "add", "path": "/multilineFieldsFormat/System.Description", "value": "Markdown"} in ops


def test_build_update_ops_no_markdown_format_op_without_description() -> None:
    ops = build_update_ops(title="New title")
    assert not any(op["path"] == "/multilineFieldsFormat/System.Description" for op in ops)


def test_html_url_present() -> None:
    work_item = {"_links": {"html": {"href": "https://dev.azure.com/org/web/wi.aspx?id=12"}}}
    assert html_url(work_item) == "https://dev.azure.com/org/web/wi.aspx?id=12"


def test_html_url_missing() -> None:
    assert html_url({}) is None


def test_edit_url() -> None:
    assert edit_url(AdoRemote(org="myorg", project="MyProj", repo="myrepo"), 42) == "https://dev.azure.com/myorg/MyProj/_workitems/edit/42"


def test_build_search_wiql_keywords_only() -> None:
    wiql = build_search_wiql(["auth"], state="all")
    assert "[System.TeamProject] = @project" in wiql
    assert "[System.Title] Contains Words 'auth'" in wiql
    assert "[System.Description] Contains Words 'auth'" in wiql
    assert "[System.ChangedDate] >=" not in wiql
    assert wiql.endswith("ORDER BY [System.ChangedDate] DESC")


def test_build_search_wiql_no_keywords_lists_without_text_filter() -> None:
    wiql = build_search_wiql([], state="all")
    assert wiql == "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project ORDER BY [System.ChangedDate] DESC"


def test_build_search_wiql_defaults_to_open_state() -> None:
    # Covers all four built-in process templates' terminal state names (Agile/CMMI: 'Closed',
    # Scrum/Basic: 'Done'; all: 'Removed') since search() doesn't know which one a project uses.
    wiql = build_search_wiql(["auth"])
    assert "[System.State] <> 'Closed'" in wiql
    assert "[System.State] <> 'Done'" in wiql
    assert "[System.State] <> 'Removed'" in wiql


def test_build_search_wiql_closed_state() -> None:
    wiql = build_search_wiql(["auth"], state="closed")
    assert "[System.State] = 'Closed'" in wiql
    assert "[System.State] = 'Done'" in wiql
    assert "[System.State] = 'Removed'" in wiql
    assert "<>" not in wiql


def test_build_search_wiql_all_state_has_no_state_clause() -> None:
    assert "System.State" not in build_search_wiql(["auth"], state="all")


def test_build_search_wiql_multiple_keywords_are_anded() -> None:
    wiql = build_search_wiql(["auth", "timeout"], state="all")
    assert wiql.count(" AND ") == 2  # TeamProject AND keyword1 AND keyword2


def test_build_search_wiql_since_adds_changed_date_clause() -> None:
    wiql = build_search_wiql(["auth"], since="2026-08-10")
    assert "[System.ChangedDate] >= '2026-08-10T00:00:00Z'" in wiql


def test_build_search_wiql_escapes_single_quotes() -> None:
    wiql = build_search_wiql(["it's broken"])
    assert "it''s broken" in wiql


def test_build_search_wiql_area_path_scopes_to_board() -> None:
    wiql = build_search_wiql(["auth"], area_path="MyProj\\Data Team")
    assert "[System.AreaPath] UNDER 'MyProj\\Data Team'" in wiql


def test_build_search_wiql_no_area_path_by_default() -> None:
    assert "AreaPath" not in build_search_wiql(["auth"])
