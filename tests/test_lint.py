from pathlib import Path

from bmsdna.devtools import lint

_TOOLING_OK = """
[project]
name = "x"
dependencies = ["pgdevkit[db]>=0.7.1"]

[dependency-groups]
dev = ["ty>=0.0.59", "ruff>=0.14.0"]
test = ["pytest>=9.1.0"]

[tool.pytest.ini_options]
pythonpath = ["."]
"""


def _setup_repo(
    tmp_path: Path, *, tooling_ok: bool = True, extra_pyproject: str = ""
) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        (_TOOLING_OK if tooling_ok else "[project]\nname = 'x'\n") + extra_pyproject
    )
    if tooling_ok:
        (tmp_path / "prek.toml").write_text("")
    return tmp_path


def test_subset_mode_only_scans_the_given_files(tmp_path: Path) -> None:
    # Simulates a prek/pre-commit hook: only the staged/changed files are passed, not the
    # rest of the repo -- a bad file that isn't in the list must not be picked up.
    _setup_repo(tmp_path)
    good = tmp_path / "good.py"
    good.write_text(
        'async def f(cur):\n    await cur.execute("select 1 from t where id = %(id)s", {"id": 1})\n'
    )
    bad = tmp_path / "bad.py"
    bad.write_text(
        'async def f(cur, v):\n    await cur.execute("select * from t where id = " + str(v))\n'
    )

    result = lint.run([str(good)], root=tmp_path, skip_tooling_check=True)
    assert result.ok

    result = lint.run([str(bad)], root=tmp_path, skip_tooling_check=True)
    assert {f.rule for f in result.findings} == {"sql-concat-injection"}


def test_missing_explicit_path_is_flagged_not_silently_clean(tmp_path: Path) -> None:
    # A prek/pre-commit hook passing a typo'd or stale (deleted/renamed) filename must not
    # report a clean pass with zero visibility that nothing was actually scanned.
    _setup_repo(tmp_path)
    result = lint.run(["nonexistent_typo.py"], root=tmp_path, skip_tooling_check=True)
    assert not result.ok
    assert [f.rule for f in result.findings] == ["lint-path-not-found"]


def test_directory_mode_walks_recursively_and_skips_venv(tmp_path: Path) -> None:
    _setup_repo(tmp_path)
    bad = tmp_path / "backend" / "db" / "repo.py"
    bad.parent.mkdir(parents=True)
    bad.write_text(
        'async def f(cur, v):\n    await cur.execute("select * from t where id = " + str(v))\n'
    )

    ignored = tmp_path / ".venv" / "lib" / "site.py"
    ignored.parent.mkdir(parents=True)
    ignored.write_text(
        'async def f(cur, v):\n    await cur.execute("select * from t where id = " + str(v))\n'
    )

    result = lint.run([], root=tmp_path, skip_tooling_check=True)
    assert len(result.findings) == 1
    assert result.findings[0].path == bad


def test_tooling_check_runs_by_default(tmp_path: Path) -> None:
    _setup_repo(tmp_path, tooling_ok=False)
    result = lint.run([], root=tmp_path)
    assert not result.tooling_skipped
    assert any(f.rule.startswith("tooling-missing-") for f in result.findings)


def test_no_tooling_check_flag_bypasses_it(tmp_path: Path) -> None:
    _setup_repo(tmp_path, tooling_ok=False)
    result = lint.run([], root=tmp_path, skip_tooling_check=True)
    assert result.tooling_skipped
    assert not any(f.rule.startswith("tooling-missing-") for f in result.findings)


def test_pyproject_opt_out_bypasses_tooling_check(tmp_path: Path) -> None:
    _setup_repo(
        tmp_path,
        tooling_ok=False,
        extra_pyproject="\n[tool.bdt.lint]\nskip_tooling_check = true\n",
    )
    result = lint.run([], root=tmp_path)
    assert result.tooling_skipped
    assert not any(f.rule.startswith("tooling-missing-") for f in result.findings)


def test_print_report_exit_codes(capsys) -> None:
    clean = lint.LintResult(findings=[], tooling_skipped=False)
    assert lint.print_report(clean) == 0

    from bmsdna.devtools.lint_findings import Finding

    dirty = lint.LintResult(
        findings=[Finding(Path("a.py"), 1, "sql-positional-param", "msg")],
        tooling_skipped=False,
    )
    assert lint.print_report(dirty) == 1
    out = capsys.readouterr().out
    assert "sql-positional-param" in out
    assert "1 issue(s) found" in out
