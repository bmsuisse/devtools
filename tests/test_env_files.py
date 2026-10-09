from pathlib import Path

from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.env_files import format_keys, get_keys


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    cwd = tmp_path / "proj"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    (cwd / ".env").write_text(
        "# comment\nDB_URL=postgres://u:secret@h/db\n\nAPI_KEY = 'topsecret'\nDB_URL=dup\n"
    )
    (cwd / "ci.env").write_text("export TOKEN=abc\n")
    (cwd / "notes.txt").write_text("NOT_AN_ENV=1\n")
    (home / "work.env").write_text("export HOME_VAR=hunter2\n")
    return cwd, home


def test_lists_keys_only_never_values(tmp_path):
    cwd, home = _setup(tmp_path)
    out = format_keys(get_keys(cwd, home), home)
    assert "DB_URL" in out and "API_KEY" in out and "TOKEN" in out and "HOME_VAR" in out
    assert "secret" not in out and "hunter2" not in out and "abc" not in out
    assert "NOT_AN_ENV" not in out
    assert out.count("DB_URL") == 1


def test_bash_syntax_hint_for_home_and_export_files(tmp_path):
    cwd, home = _setup(tmp_path)
    out = format_keys(get_keys(cwd, home), home)
    assert "source ~/work.env" in out
    assert "source " + str(cwd / "ci.env") in out
    assert f"source {cwd / '.env'}" not in out


def test_search_filters_keys_and_drops_empty_files(tmp_path):
    cwd, home = _setup(tmp_path)
    files = get_keys(cwd, home, "api")
    assert [f.keys for f in files] == [["API_KEY"]]


def test_cli_exit_1_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    res = CliRunner().invoke(app, ["env", "get-keys"])
    assert res.exit_code == 1


def test_cli_prints_keys(tmp_path, monkeypatch):
    cwd, home = _setup(tmp_path)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))
    res = CliRunner().invoke(app, ["env", "get-keys", "--search", "token"])
    assert res.exit_code == 0
    assert "TOKEN" in res.output and "abc" not in res.output
