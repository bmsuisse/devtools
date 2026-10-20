from bmsdna.devtools.bdt_config import find_pyproject, load_bdt_table


def write_pyproject(tmp_path, body: str):
    (tmp_path / "pyproject.toml").write_text(body)
    return tmp_path


def test_find_pyproject_none_when_absent(tmp_path) -> None:
    assert find_pyproject(tmp_path) is None


def test_find_pyproject_searches_parent_directories(tmp_path) -> None:
    write_pyproject(tmp_path, "[project]\nname = 'x'\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_pyproject(nested) == tmp_path / "pyproject.toml"


def test_load_bdt_table_returns_empty_when_no_pyproject(tmp_path) -> None:
    assert load_bdt_table("ado", tmp_path) == {}


def test_load_bdt_table_returns_empty_when_key_absent(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.bdt.envs.prod]\nwebapp = 'x'\nresource_group = 'y'\n")
    assert load_bdt_table("ado", tmp_path) == {}


def test_load_bdt_table_reads_configured_table(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.bdt.ado]\nboard = 'My Team'\n")
    assert load_bdt_table("ado", tmp_path) == {"board": "My Team"}


def test_find_pyproject_stops_at_the_repo_root(tmp_path) -> None:
    """A submodule (its `.git` is a file) is another repository: the parent repo's config must not apply."""
    write_pyproject(tmp_path, "[tool.bdt.pr.required_labels]\ne2e_area = ['e2e-full']\n")
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "database"
    (sub / "src").mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../.git/modules/database\n")

    assert find_pyproject(sub / "src") is None
    assert load_bdt_table("pr", sub / "src") == {}


def test_find_pyproject_prefers_the_repo_roots_own_pyproject(tmp_path) -> None:
    write_pyproject(tmp_path, "[project]\nname = 'x'\n")
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert find_pyproject(nested) == tmp_path / "pyproject.toml"
