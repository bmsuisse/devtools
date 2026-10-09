from typer.testing import CliRunner

from bmsdna.devtools.cli import app

runner = CliRunner()


def test_pull_cli_defaults(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr("bmsdna.devtools.cli.pull_mod.run", lambda **kwargs: captured.update(kwargs))

    result = runner.invoke(app, ["pull"])

    assert result.exit_code == 0, result.output
    assert captured == {
        "remote": "origin",
        "no_default": False,
        "dry_run": False,
        "pull_args": [],
    }


def test_pull_cli_passes_options_through(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr("bmsdna.devtools.cli.pull_mod.run", lambda **kwargs: captured.update(kwargs))

    result = runner.invoke(app, ["pull", "--remote", "upstream", "--no-default", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert captured == {
        "remote": "upstream",
        "no_default": True,
        "dry_run": True,
        "pull_args": [],
    }


def test_pull_cli_passes_through_extra_git_pull_flags_after_double_dash(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr("bmsdna.devtools.cli.pull_mod.run", lambda **kwargs: captured.update(kwargs))

    result = runner.invoke(app, ["pull", "--", "--rebase", "--ff-only"])

    assert result.exit_code == 0, result.output
    assert captured["pull_args"] == ["--rebase", "--ff-only"]
