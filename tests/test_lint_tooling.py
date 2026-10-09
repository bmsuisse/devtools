from pathlib import Path

from bmsdna.devtools import lint_tooling
from bmsdna.devtools.lint_tooling import check_tooling


def _write_pyproject(tmp_path: Path, body: str) -> None:
    (tmp_path / "pyproject.toml").write_text(body)


_FULLY_CONFIGURED = """
[project]
name = "x"
dependencies = ["pgdevkit[db]>=0.7.1"]

[dependency-groups]
dev = ["ty>=0.0.59", "ruff>=0.14.0"]
test = ["pytest>=9.1.0"]

[tool.pytest.ini_options]
pythonpath = ["."]
"""


def test_no_pyproject_is_flagged(tmp_path: Path, monkeypatch) -> None:
    # find_pyproject() walks up parent directories (like git's own search) -- stub it out so
    # this test is deterministic regardless of whether some ancestor of tmp_path (e.g. a stray
    # file under /tmp) happens to have a pyproject.toml of its own.
    monkeypatch.setattr(lint_tooling, "find_pyproject", lambda root: None)
    findings = check_tooling(tmp_path)
    assert [f.rule for f in findings] == ["tooling-missing-pyproject"]


def test_missing_dependencies_are_each_flagged(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "[project]\nname = 'x'\n")
    rules = {f.rule for f in check_tooling(tmp_path)}
    assert rules == {
        "tooling-missing-ty",
        "tooling-missing-ruff",
        "tooling-missing-pytest",
        "tooling-missing-prek",
    }


def test_pytest_declared_but_not_configured_is_flagged(tmp_path: Path) -> None:
    _write_pyproject(
        tmp_path,
        """
[project]
name = "x"

[dependency-groups]
dev = ["ty", "ruff"]
test = ["pytest"]
""",
    )
    rules = {f.rule for f in check_tooling(tmp_path)}
    assert "tooling-missing-pytest-config" in rules
    assert "tooling-missing-pytest" not in rules


def test_prek_toml_missing_is_flagged(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, _FULLY_CONFIGURED)
    rules = {f.rule for f in check_tooling(tmp_path)}
    assert rules == {"tooling-missing-prek"}


def test_fully_configured_repo_is_clean(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, _FULLY_CONFIGURED)
    (tmp_path / "prek.toml").write_text("")
    assert check_tooling(tmp_path) == []


def test_pytest_ini_file_satisfies_pytest_config(tmp_path: Path) -> None:
    _write_pyproject(
        tmp_path,
        """
[project]
name = "x"

[dependency-groups]
dev = ["ty", "ruff"]
test = ["pytest"]
""",
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "prek.toml").write_text("")
    assert check_tooling(tmp_path) == []
