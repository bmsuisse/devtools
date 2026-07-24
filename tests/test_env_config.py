import pytest

from bmsdna.devtools.env_config import load_envs, resolve_env


def write_pyproject(tmp_path, body: str):
    (tmp_path / "pyproject.toml").write_text(body)
    return tmp_path


def test_load_envs_returns_empty_when_no_pyproject(tmp_path):
    assert load_envs(tmp_path) == {}


def test_load_envs_returns_empty_when_no_bdt_table(tmp_path):
    write_pyproject(tmp_path, "[project]\nname = 'x'\n")
    assert load_envs(tmp_path) == {}


def test_load_envs_parses_configured_environments(tmp_path):
    write_pyproject(
        tmp_path,
        """
[tool.bdt.envs.prod]
webapp = "my-app"
resource_group = "my-app-rg"
slot = "production"
""",
    )
    envs = load_envs(tmp_path)
    assert envs == {"prod": {"webapp": "my-app", "resource_group": "my-app-rg", "slot": "production"}}


def test_load_envs_searches_parent_directories(tmp_path):
    write_pyproject(
        tmp_path,
        """
[tool.bdt.envs.prod]
webapp = "my-app"
resource_group = "my-app-rg"
slot = "production"
""",
    )
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert "prod" in load_envs(nested)


def test_resolve_env_exits_when_no_envs_configured(tmp_path):
    with pytest.raises(SystemExit, match="no environments configured"):
        resolve_env("prod", tmp_path)


def test_resolve_env_exits_on_unknown_name(tmp_path):
    write_pyproject(
        tmp_path,
        """
[tool.bdt.envs.staging]
webapp = "my-app"
resource_group = "my-app-rg"
slot = "staging"
""",
    )
    with pytest.raises(SystemExit, match="unknown --env 'prod'.*staging"):
        resolve_env("prod", tmp_path)


def test_resolve_env_exits_when_required_key_missing(tmp_path):
    write_pyproject(
        tmp_path,
        """
[tool.bdt.envs.prod]
webapp = "my-app"
slot = "production"
""",
    )
    with pytest.raises(SystemExit, match="missing required key.*resource_group"):
        resolve_env("prod", tmp_path)


def test_resolve_env_returns_matching_env(tmp_path):
    write_pyproject(
        tmp_path,
        """
[tool.bdt.envs.prod]
webapp = "my-app"
resource_group = "my-app-rg"
slot = "production"
""",
    )
    assert resolve_env("prod", tmp_path) == {
        "webapp": "my-app",
        "resource_group": "my-app-rg",
        "slot": "production",
    }
