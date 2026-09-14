from pathlib import Path

from pgdevkit.testdb import constants as pgdevkit_constants
from typer.testing import CliRunner

from bmsdna.devtools.cli import app

runner = CliRunner()


def test_cleanup_worktrees_cli_passes_options_through(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.worktree_mod.clean_worktrees",
        lambda root, **kwargs: captured.update(root=root, **kwargs),
    )

    result = runner.invoke(
        app,
        ["cleanup", "worktrees", str(tmp_path), "--remote", "upstream", "--keep-dbs", "--yes", "--pg-port", "54322", "--pg-user", "tester"],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "root": Path(tmp_path),
        "remote": "upstream",
        "keep_dbs": True,
        "yes": True,
        "pg_port": 54322,
        "pg_user": "tester",
    }


def test_cleanup_worktrees_cli_falls_back_to_pgdevkits_own_user_when_no_pg_user_given(monkeypatch, tmp_path) -> None:
    # No --pg-user and no PGUSER/USER/LOGNAME env var: falls back to
    # pgdevkit's own test-container user, not the current OS user -- these
    # DBs were created by pgdevkit in the first place, so its own default is
    # the one actually likely to work.
    for var in ("PGUSER", "USER", "LOGNAME"):
        monkeypatch.delenv(var, raising=False)

    captured: dict = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.worktree_mod.clean_worktrees",
        lambda root, **kwargs: captured.update(root=root, **kwargs),
    )

    result = runner.invoke(app, ["cleanup", "worktrees", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert captured["pg_user"] == pgdevkit_constants.USER


def test_cleanup_orphaned_dbs_cli_passes_options_through(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.worktree_mod.clean_orphaned_dbs",
        lambda root, **kwargs: captured.update(root=root, **kwargs),
    )

    result = runner.invoke(
        app,
        ["cleanup", "orphaned-dbs", str(tmp_path), "--include-caution", "--yes", "--pg-user", "tester"],
    )

    assert result.exit_code == 0, result.output
    assert captured["include_caution"] is True
    assert captured["yes"] is True
    assert captured["pg_user"] == "tester"
