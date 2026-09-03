from bmsdna.devtools.ado_issue import build_attach_ops, build_create_ops, build_update_ops, html_url, resolve_board


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
        "/fields/System.AreaPath": "Proj\\Team",
        "/fields/System.Tags": "a; b",
        "/fields/System.State": "Resolved",
    }


def test_build_update_ops_empty_tags_list_clears_tags() -> None:
    # An explicit [] (distinct from the default None) is a deliberate "clear all tags".
    ops = build_update_ops(tags=[])
    assert ops == [{"op": "add", "path": "/fields/System.Tags", "value": ""}]


def test_html_url_present() -> None:
    work_item = {"_links": {"html": {"href": "https://dev.azure.com/org/web/wi.aspx?id=12"}}}
    assert html_url(work_item) == "https://dev.azure.com/org/web/wi.aspx?id=12"


def test_html_url_missing() -> None:
    assert html_url({}) is None
