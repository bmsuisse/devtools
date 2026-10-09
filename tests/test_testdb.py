import subprocess
from unittest.mock import MagicMock

import pytest
from pgdevkit.testdb import constants as pgdevkit_constants

from bmsdna.devtools.testdb import (
    confirm_remote_host,
    db_nested_projects,
    drop_database,
    find_orphaned,
    has_pgdevkit_project,
    is_caution_db,
    is_local_host,
    project_name,
    project_roots,
    workspace_db_names,
)


def write_pyproject(path, body: str):
    (path / "pyproject.toml").write_text(body)
    return path


def init_git_repo(path, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q", "-b", branch],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(cmd, cwd=path, check=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


# --- has_pgdevkit_project ---------------------------------------------------


def test_has_pgdevkit_project_false_when_no_pyproject(tmp_path) -> None:
    assert has_pgdevkit_project(tmp_path) is False


def test_has_pgdevkit_project_false_when_no_pgdevkit_section(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.bdt.ado]\nboard = 'x'\n")
    assert has_pgdevkit_project(tmp_path) is False


def test_has_pgdevkit_project_false_for_non_postgres_engine(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.pgdevkit]\nname = 'distributionplan'\nengine = 'mssql'\n")
    assert has_pgdevkit_project(tmp_path) is False


def test_has_pgdevkit_project_true_for_postgres_engine(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.pgdevkit]\nname = 'ccmt'\n")
    assert has_pgdevkit_project(tmp_path) is True


def test_has_pgdevkit_project_true_for_an_entirely_empty_section(tmp_path) -> None:
    # An empty `[tool.pgdevkit]` (accepting every default: name from the
    # directory, engine=postgres, ...) parses to `{}`, which is falsy but
    # still means "opted in" -- must not be confused with "no section at
    # all" (see has_pgdevkit_project's `is None` check).
    write_pyproject(tmp_path, "[tool.pgdevkit]\n")
    assert has_pgdevkit_project(tmp_path) is True


# --- db_nested_projects ------------------------------------------------------


def test_db_nested_projects_empty_when_not_configured(tmp_path) -> None:
    assert db_nested_projects(tmp_path) == []


def test_db_nested_projects_reads_configured_list(tmp_path) -> None:
    write_pyproject(tmp_path, '[tool.bdt.worktree]\ndb_nested_projects = ["akeneo_editor"]\n')
    assert db_nested_projects(tmp_path) == ["akeneo_editor"]


# --- project_roots / project_name -------------------------------------------


def test_project_roots_empty_when_not_a_pgdevkit_project(tmp_path) -> None:
    assert project_roots(tmp_path) == []


def test_project_roots_is_just_the_repo_with_no_nested_projects_configured(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.pgdevkit]\nname = 'ccmt'\n")
    assert project_roots(tmp_path) == [tmp_path]


def test_project_roots_includes_a_configured_nested_project_that_has_its_own_pgdevkit_section(tmp_path) -> None:
    write_pyproject(tmp_path, '[tool.pgdevkit]\nname = "mdm"\n\n[tool.bdt.worktree]\ndb_nested_projects = ["akeneo_editor"]\n')
    nested = tmp_path / "akeneo_editor"
    nested.mkdir()
    write_pyproject(nested, "[tool.pgdevkit]\n")

    assert project_roots(tmp_path) == [tmp_path, nested]


def test_project_roots_includes_a_configured_nested_project_with_no_pgdevkit_section_at_all(tmp_path) -> None:
    # Mirrors MDMApp's real akeneo_editor/: a pyproject.toml with no
    # [tool.pgdevkit] section of its own at all. Being named in
    # db_nested_projects is itself the opt-in -- pgdevkit's own
    # load_config() falls back to the directory name for the rest.
    write_pyproject(tmp_path, '[tool.pgdevkit]\nname = "mdm"\n\n[tool.bdt.worktree]\ndb_nested_projects = ["akeneo_editor"]\n')
    nested = tmp_path / "akeneo_editor"
    nested.mkdir()
    write_pyproject(nested, '[project]\nname = "akeneo-editor"\n')

    assert project_roots(tmp_path) == [tmp_path, nested]


def test_project_roots_excludes_a_configured_nested_project_directory_that_does_not_exist(tmp_path) -> None:
    write_pyproject(tmp_path, '[tool.pgdevkit]\nname = "mdm"\n\n[tool.bdt.worktree]\ndb_nested_projects = ["akeneo_editor"]\n')

    assert project_roots(tmp_path) == [tmp_path]


def test_project_name_none_when_not_a_pgdevkit_project(tmp_path) -> None:
    assert project_name(tmp_path) is None


def test_project_name_reads_configured_name(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.pgdevkit]\nname = 'ccmt'\n")
    assert project_name(tmp_path) == "ccmt"


def test_project_name_falls_back_to_directory_name(tmp_path) -> None:
    # Mirrors pgdevkit's own load_config() fallback (see its README): a
    # `[tool.pgdevkit]` section with no explicit `name` uses the containing
    # directory's name instead.
    akeneo_editor = tmp_path / "akeneo_editor"
    akeneo_editor.mkdir()
    write_pyproject(akeneo_editor, "[tool.pgdevkit]\n")
    assert project_name(akeneo_editor) == "akeneo_editor"


# --- workspace_db_names (real pgdevkit calls -- no DB connection needed,   --
# --- pure naming from git branch + pyproject.toml)                        --


def test_workspace_db_names_is_just_the_main_db_with_no_extra_config(tmp_path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_pyproject(repo, "[tool.pgdevkit]\nname = 'ccmt'\n")
    assert workspace_db_names(repo) == {"ccmt_main"}


def test_workspace_db_names_includes_pgdevkits_own_extra_db_suffixes(tmp_path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_pyproject(repo, '[tool.pgdevkit]\nname = "ccmt"\nextra_db_suffixes = ["_onetrade"]\n')
    assert workspace_db_names(repo) == {"ccmt_main", "ccmt_main_onetrade"}


def test_workspace_db_names_includes_a_configured_nested_project_on_the_same_branch(tmp_path) -> None:
    # Mirrors MDMApp: akeneo_editor/ is a real subdirectory sharing the
    # parent worktree's git checkout (and so its branch), with its own
    # pyproject.toml/[tool.pgdevkit] section.
    repo = init_git_repo(tmp_path / "repo", branch="my-feature")
    write_pyproject(repo, '[tool.pgdevkit]\nname = "mdm"\n\n[tool.bdt.worktree]\ndb_nested_projects = ["akeneo_editor"]\n')
    nested = repo / "akeneo_editor"
    nested.mkdir()
    write_pyproject(nested, "[tool.pgdevkit]\n")

    assert workspace_db_names(repo) == {"mdm_my_feature", "akeneo_editor_my_feature"}


def test_workspace_db_names_includes_a_nested_project_with_no_pgdevkit_section_at_all(tmp_path) -> None:
    # Mirrors the real MDMApp/akeneo_editor case exactly (see project_roots'
    # docstring): the nested project's pyproject.toml has no
    # [tool.pgdevkit] section, only a bare [project] table.
    repo = init_git_repo(tmp_path / "repo", branch="my-feature")
    write_pyproject(repo, '[tool.pgdevkit]\nname = "mdm"\n\n[tool.bdt.worktree]\ndb_nested_projects = ["akeneo_editor"]\n')
    nested = repo / "akeneo_editor"
    nested.mkdir()
    write_pyproject(nested, '[project]\nname = "akeneo-editor"\n')

    assert workspace_db_names(repo) == {"mdm_my_feature", "akeneo_editor_my_feature"}


def test_workspace_db_names_empty_when_not_a_pgdevkit_project(tmp_path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    assert workspace_db_names(repo) == frozenset()


# --- find_orphaned -----------------------------------------------------------


def test_find_orphaned_delegates_to_pgdevkit_per_project_root(tmp_path, monkeypatch) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_pyproject(repo, "[tool.pgdevkit]\nname = 'ccmt'\n")

    monkeypatch.setattr(
        "bmsdna.devtools.testdb.pgdevkit_testdb.find_orphaned_dbs",
        lambda project_root: ["ccmt_old_removed_feature", "ccmt_dev"],
    )

    assert find_orphaned(repo) == [
        ("ccmt_old_removed_feature", "ccmt", False),
        ("ccmt_dev", "ccmt", True),
    ]


def test_find_orphaned_covers_a_configured_nested_project_too(tmp_path, monkeypatch) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_pyproject(repo, '[tool.pgdevkit]\nname = "mdm"\n\n[tool.bdt.worktree]\ndb_nested_projects = ["akeneo_editor"]\n')
    nested = repo / "akeneo_editor"
    nested.mkdir()
    write_pyproject(nested, "[tool.pgdevkit]\n")

    def fake_find_orphaned_dbs(project_root):
        return ["mdm_ghost"] if project_root == repo else ["akeneo_editor_ghost"]

    monkeypatch.setattr("bmsdna.devtools.testdb.pgdevkit_testdb.find_orphaned_dbs", fake_find_orphaned_dbs)

    assert find_orphaned(repo) == [
        ("mdm_ghost", "mdm", False),
        ("akeneo_editor_ghost", "akeneo_editor", False),
    ]


def test_find_orphaned_empty_when_not_a_pgdevkit_project(tmp_path, monkeypatch) -> None:
    repo = init_git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        "bmsdna.devtools.testdb.pgdevkit_testdb.find_orphaned_dbs",
        lambda project_root: pytest.fail("should not be called for a non-pgdevkit repo"),
    )
    assert find_orphaned(repo) == []


# --- is_caution_db -----------------------------------------------------------


def test_is_caution_db_flags_bare_branch_name_suffix() -> None:
    assert is_caution_db("ccmt_main", "ccmt", []) is True
    assert is_caution_db("ccmt_test", "ccmt", []) is True


def test_is_caution_db_false_for_slugified_feature_branch() -> None:
    assert is_caution_db("ccmt_bonus_rule_customer_group", "ccmt", []) is False


def test_is_caution_db_strips_configured_sibling_suffix_before_checking() -> None:
    assert is_caution_db("ccmt_main_onetrade", "ccmt", ["_onetrade"]) is True


def test_is_caution_db_false_when_project_prefix_does_not_match() -> None:
    assert is_caution_db("mdm_main", "ccmt", []) is False


def test_is_caution_db_slugifies_project_name_before_comparing() -> None:
    # db_name is always pgdevkit's *slugified* name (e.g. "mdmapp_main" for
    # project name "MDMApp") -- comparing against the raw, unslugified
    # project name would never match and silently never flag anything.
    assert is_caution_db("mdmapp_main", "MDMApp", []) is True
    assert is_caution_db("mdmapp_test", "MDMApp", []) is True


# --- drop_database -----------------------------------------------------------


def test_drop_database_issues_drop_database_if_exists(monkeypatch) -> None:
    captured_cmd: list[str] = []

    monkeypatch.setattr("bmsdna.devtools.testdb.require_psql", lambda: "/usr/bin/psql")

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.testdb.subprocess.run", fake_run)

    drop_database("ccmt_my_feature", pg_host="localhost", pg_port=54322, pg_user="tester")

    assert captured_cmd[0] == "/usr/bin/psql"
    assert "-h" in captured_cmd
    assert captured_cmd[captured_cmd.index("-h") + 1] == "localhost"
    assert "-c" in captured_cmd
    assert captured_cmd[captured_cmd.index("-c") + 1] == 'DROP DATABASE IF EXISTS "ccmt_my_feature"'


def test_drop_database_authenticates_with_pgdevkits_own_password_over_tcp(monkeypatch) -> None:
    # No -h means psql defaults to the unix socket (peer auth), which
    # doesn't match how pgdevkit's own find_orphaned_dbs()/
    # workspace_db_names() connect (TCP + password) -- so the listing half
    # and this DROP step could silently target two different Postgres
    # instances, or fail outright on peer auth.
    captured_env: dict = {}

    monkeypatch.setattr("bmsdna.devtools.testdb.require_psql", lambda: "/usr/bin/psql")

    def fake_run(cmd, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.testdb.subprocess.run", fake_run)

    drop_database("ccmt_my_feature", pg_host="localhost", pg_port=54322, pg_user="tester")

    assert captured_env["PGPASSWORD"] == pgdevkit_constants.PASSWORD


def test_drop_database_escapes_embedded_double_quotes_in_db_name(monkeypatch) -> None:
    # `name` comes from a live pg_database listing, not a bdt-validated
    # slug -- an unescaped embedded `"` would let it break out of the
    # quoted identifier and inject a second SQL statement into `psql -c`.
    captured_cmd: list[str] = []

    monkeypatch.setattr("bmsdna.devtools.testdb.require_psql", lambda: "/usr/bin/psql")

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("bmsdna.devtools.testdb.subprocess.run", fake_run)

    drop_database('evil"; drop database postgres; --', pg_host="localhost", pg_port=54322, pg_user="tester")

    dropped_sql = captured_cmd[captured_cmd.index("-c") + 1]
    assert dropped_sql == 'DROP DATABASE IF EXISTS "evil""; drop database postgres; --"'


def test_drop_database_exits_with_a_friendly_message_when_psql_is_missing(monkeypatch) -> None:
    def fake_require_psql():
        raise SystemExit("'psql' is required for this command but wasn't found on PATH.\nsome hint")

    monkeypatch.setattr("bmsdna.devtools.testdb.require_psql", fake_require_psql)

    with pytest.raises(SystemExit, match="'psql' is required"):
        drop_database("ccmt_my_feature", pg_host="localhost", pg_port=54322, pg_user="tester")


# --- is_local_host / confirm_remote_host -------------------------------------


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_is_local_host_true_for_local_addresses(host: str) -> None:
    assert is_local_host(host) is True


@pytest.mark.parametrize("host", ["db.example.com", "10.0.0.5", "prod-postgres"])
def test_is_local_host_false_for_non_local_addresses(host: str) -> None:
    assert is_local_host(host) is False


def test_confirm_remote_host_skips_prompt_for_local_host(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(AssertionError("should not prompt for a local host")))

    assert confirm_remote_host("localhost", 54322) is True


def test_confirm_remote_host_requires_typed_yes_for_remote_host(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda *_: "yes")

    assert confirm_remote_host("db.example.com", 5432) is True


def test_confirm_remote_host_rejects_anything_other_than_yes(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda *_: "y")

    assert confirm_remote_host("db.example.com", 5432) is False


def test_confirm_remote_host_fails_closed_when_input_is_not_interactive(monkeypatch) -> None:
    def raise_eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    assert confirm_remote_host("db.example.com", 5432) is False
