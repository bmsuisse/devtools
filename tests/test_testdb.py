import hashlib
from unittest.mock import MagicMock

from bmsdna.devtools.testdb import (
    db_nested_projects,
    db_sibling_suffixes,
    drop_database,
    expected_db_names,
    is_caution_db,
    list_databases,
    read_pgdevkit_project,
    slugify,
    workspace_db_name,
)


def write_pyproject(tmp_path, body: str):
    (tmp_path / "pyproject.toml").write_text(body)
    return tmp_path


def test_slugify_lowercases_and_collapses_invalid_chars() -> None:
    assert slugify("Bonus Rule/Customer-Group!!") == "bonus_rule_customer_group"


def test_slugify_strips_leading_and_trailing_underscores() -> None:
    assert slugify("--branch--") == "branch"


def test_slugify_falls_back_to_x_when_nothing_valid_remains() -> None:
    assert slugify("!!!") == "x"


def test_slugify_hash_truncates_anything_over_30_chars() -> None:
    long_value = "a" * 40
    result = slugify(long_value)
    expected_digest = hashlib.sha256(("a" * 40).encode()).hexdigest()[:8]
    assert result == f"{'a' * 30}_{expected_digest}"
    assert len(result) == 39


def test_slugify_leaves_exactly_30_chars_untouched() -> None:
    value = "a" * 30
    assert slugify(value) == value


def test_workspace_db_name_joins_and_reslugifies_project_and_branch() -> None:
    assert workspace_db_name("ccmt", "bonus-rule-customer-group") == "ccmt_bonus_rule_customer_group"


def test_workspace_db_name_hash_truncates_long_joined_result() -> None:
    name = workspace_db_name("mdm", "akeneo-editor-impersonation")
    assert name.startswith("mdm_akeneo_editor_impersonatio_")
    assert len(name) == len("mdm_akeneo_editor_impersonatio") + 1 + 8


def test_read_pgdevkit_project_none_when_no_pyproject(tmp_path) -> None:
    assert read_pgdevkit_project(tmp_path) is None


def test_read_pgdevkit_project_none_when_no_pgdevkit_section(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.bdt.ado]\nboard = 'x'\n")
    assert read_pgdevkit_project(tmp_path) is None


def test_read_pgdevkit_project_none_for_non_postgres_engine(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.pgdevkit]\nname = 'distributionplan'\nengine = 'mssql'\n")
    assert read_pgdevkit_project(tmp_path) is None


def test_read_pgdevkit_project_reads_configured_name(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.pgdevkit]\nname = 'ccmt'\n")
    assert read_pgdevkit_project(tmp_path) == "ccmt"


def test_read_pgdevkit_project_falls_back_to_directory_name(tmp_path) -> None:
    """Mirrors pgdevkit's own load_config() fallback: a `[tool.pgdevkit]`
    section with no explicit `name` (e.g. akeneo_editor/'s own pyproject.toml)
    uses the containing directory's name instead."""
    akeneo_editor = tmp_path / "akeneo_editor"
    akeneo_editor.mkdir()
    write_pyproject(akeneo_editor, "[tool.pgdevkit]\nengine = 'postgres'\n")
    assert read_pgdevkit_project(akeneo_editor) == "akeneo_editor"


def test_db_sibling_suffixes_empty_when_not_configured(tmp_path) -> None:
    assert db_sibling_suffixes(tmp_path) == []


def test_db_sibling_suffixes_reads_configured_list(tmp_path) -> None:
    write_pyproject(tmp_path, '[tool.bdt.worktree]\ndb_sibling_suffixes = ["_onetrade"]\n')
    assert db_sibling_suffixes(tmp_path) == ["_onetrade"]


def test_db_nested_projects_reads_configured_list(tmp_path) -> None:
    write_pyproject(tmp_path, '[tool.bdt.worktree]\ndb_nested_projects = ["akeneo_editor"]\n')
    assert db_nested_projects(tmp_path) == ["akeneo_editor"]


def test_expected_db_names_includes_main_db_only_with_no_conventions_configured(tmp_path) -> None:
    assert expected_db_names(tmp_path, "ccmt", "my-feature") == {"ccmt_my_feature"}


def test_expected_db_names_includes_configured_sibling_suffix(tmp_path) -> None:
    write_pyproject(tmp_path, '[tool.bdt.worktree]\ndb_sibling_suffixes = ["_onetrade"]\n')
    names = expected_db_names(tmp_path, "ccmt", "my-feature")
    assert names == {"ccmt_my_feature", "ccmt_my_feature_onetrade"}


def test_expected_db_names_includes_configured_nested_project(tmp_path) -> None:
    write_pyproject(tmp_path, '[tool.bdt.worktree]\ndb_nested_projects = ["akeneo_editor"]\n')
    names = expected_db_names(tmp_path, "mdm", "my-feature")
    assert names == {"mdm_my_feature", "akeneo_editor_my_feature"}


def test_is_caution_db_flags_bare_branch_name_suffix() -> None:
    assert is_caution_db("ccmt_main", "ccmt", []) is True
    assert is_caution_db("ccmt_test", "ccmt", []) is True


def test_is_caution_db_false_for_slugified_feature_branch() -> None:
    assert is_caution_db("ccmt_bonus_rule_customer_group", "ccmt", []) is False


def test_is_caution_db_strips_configured_sibling_suffix_before_checking() -> None:
    assert is_caution_db("ccmt_main_onetrade", "ccmt", ["_onetrade"]) is True


def test_is_caution_db_false_when_project_prefix_does_not_match() -> None:
    assert is_caution_db("mdm_main", "ccmt", []) is False


def test_list_databases_parses_psql_output(monkeypatch) -> None:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="ccmt_main\nccmt_my_feature\n\n")

    monkeypatch.setattr("bmsdna.devtools.testdb.subprocess.run", fake_run)

    result = list_databases(pg_port=54322, pg_user="tester")

    assert result == ["ccmt_main", "ccmt_my_feature"]
    assert "-p" in captured_cmd and captured_cmd[captured_cmd.index("-p") + 1] == "54322"
    assert "-U" in captured_cmd and captured_cmd[captured_cmd.index("-U") + 1] == "tester"


def test_list_databases_empty_on_psql_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.testdb.subprocess.run",
        lambda cmd, **kwargs: MagicMock(returncode=1, stdout=""),
    )
    assert list_databases(pg_port=54322, pg_user="tester") == []


def test_drop_database_issues_drop_database_if_exists(monkeypatch) -> None:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.testdb.subprocess.run", fake_run)

    drop_database("ccmt_my_feature", pg_port=54322, pg_user="tester")

    assert "-c" in captured_cmd
    assert captured_cmd[captured_cmd.index("-c") + 1] == 'DROP DATABASE IF EXISTS "ccmt_my_feature"'
