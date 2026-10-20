from typer.testing import CliRunner

from bmsdna.devtools.cli import __version__, app

runner = CliRunner()


def test_version_shows_update_hint() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == f"bdt {__version__}"
    assert "uv tool upgrade bmsdna-devtools" in result.output
    assert "uv sync --upgrade-package bmsdna-devtools" in result.output
