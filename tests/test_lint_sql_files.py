from pathlib import Path

import pytest

from bmsdna.devtools import dead_code
from bmsdna.devtools.bdt_config import load_bdt_table
from bmsdna.devtools.api_usage import ApiUsageError
from bmsdna.devtools.lint_sql_files import check_unreferenced_sql_files


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _check(root: Path, *, sql_root: str = "backend", **kwargs) -> list[str]:
    sql_files = sorted((root / sql_root).rglob("*.sql"))
    findings = check_unreferenced_sql_files(repo_root=root, sql_files=sql_files, python_files=sorted(root.rglob("*.py")), **kwargs)
    return [f.path.relative_to(root).as_posix() for f in findings if f.rule == "sql-file-unreferenced"]


def test_literal_load_sql_call_references_file(tmp_path: Path) -> None:
    _write(tmp_path, "backend/queries/customers/get_all.sql", "select 1")
    _write(tmp_path, "backend/queries/customers/dead.sql", "select 2")
    _write(tmp_path, "backend/repo.py", 'q = load_sql("customers", "get_all")\n')
    assert _check(tmp_path) == ["backend/queries/customers/dead.sql"]


def test_same_stem_in_other_topic_does_not_count(tmp_path: Path) -> None:
    _write(tmp_path, "backend/queries/a/get.sql", "select 1")
    _write(tmp_path, "backend/queries/b/get.sql", "select 1")
    _write(tmp_path, "backend/repo.py", 'load_sql("a", "get")\n')
    assert _check(tmp_path) == ["backend/queries/b/get.sql"]


def test_dynamic_name_counts_stems_literal_in_same_file(tmp_path: Path) -> None:
    _write(tmp_path, "backend/queries/rel/by_a.sql")
    _write(tmp_path, "backend/queries/rel/by_b.sql")
    _write(tmp_path, "backend/queries/rel/unused.sql")
    _write(
        tmp_path,
        "backend/repo.py",
        'name = "by_a" if x else "by_b"\nload_sql("rel", name)\n',
    )
    assert _check(tmp_path) == ["backend/queries/rel/unused.sql"]


def test_dynamic_name_ignores_literals_from_other_files(tmp_path: Path) -> None:
    _write(tmp_path, "backend/queries/rel/by_a.sql")
    _write(tmp_path, "backend/repo.py", 'load_sql("rel", name)\n')
    _write(tmp_path, "backend/other.py", 'x = "by_a"\n')
    assert _check(tmp_path) == ["backend/queries/rel/by_a.sql"]


def test_dynamic_topic_references_any_stem_named_in_file(tmp_path: Path) -> None:
    _write(tmp_path, "backend/queries/t/x.sql")
    _write(tmp_path, "backend/queries/t/y.sql")
    _write(tmp_path, "backend/repo.py", 'load_sql(topic, "x")\n')
    assert _check(tmp_path) == ["backend/queries/t/y.sql"]


def test_repo_relative_path_literal_references_file(tmp_path: Path) -> None:
    _write(tmp_path, "backend/api/sql/print/conditions.sql")
    _write(tmp_path, "backend/api/sql/print/other.sql")
    _write(
        tmp_path,
        "backend/print.py",
        'get_sql_with_prm_list("backend/api/sql/print/conditions.sql")\n',
    )
    assert _check(tmp_path) == ["backend/api/sql/print/other.sql"]


def test_bare_filename_only_counts_inside_sql_folders_parent(tmp_path: Path) -> None:
    _write(tmp_path, "backend/app/dedup/sql/active.sql")
    _write(tmp_path, "backend/app/dedup/sql/lonely.sql")
    _write(
        tmp_path,
        "backend/app/dedup/svc.py",
        '_SQL = Path(__file__).parent / "sql"\nq = (_SQL / "active.sql").read_text()\n',
    )
    _write(
        tmp_path,
        "backend/app/elsewhere/other.py",
        'q = (D / "lonely.sql").read_text()\n',
    )
    assert _check(tmp_path) == ["backend/app/dedup/sql/lonely.sql"]


def test_custom_loader_function_name(tmp_path: Path) -> None:
    _write(tmp_path, "backend/q/t/a.sql")
    _write(tmp_path, "backend/repo.py", 'get_query("t", "a")\n')
    assert _check(tmp_path) == ["backend/q/t/a.sql"]
    assert _check(tmp_path, loader_functions=["get_query"]) == []


def test_method_call_on_loader_object_is_recognised(tmp_path: Path) -> None:
    _write(tmp_path, "backend/q/t/a.sql")
    _write(tmp_path, "backend/repo.py", 'loader.load_sql("t", "a")\n')
    assert _check(tmp_path) == []


def test_ignore_globs_suppress_finding(tmp_path: Path) -> None:
    _write(tmp_path, "backend/q/t/a.sql")
    assert _check(tmp_path, ignore_globs=["backend/q/t/*.sql"]) == []


def test_keyword_arguments_to_loader_are_recognised(tmp_path: Path) -> None:
    _write(tmp_path, "backend/q/a/b.sql")
    _write(tmp_path, "backend/q/a/c.sql")
    _write(tmp_path, "backend/repo.py", 'load_sql(topic="a", name="b")\n')
    assert _check(tmp_path) == ["backend/q/a/c.sql"]


def test_unresolvable_loader_call_is_treated_as_dynamic(tmp_path: Path) -> None:
    _write(tmp_path, "backend/q/a/b.sql")
    _write(tmp_path, "backend/repo.py", 'load_sql(**spec)\nx = "b"\n')
    assert _check(tmp_path) == []


def test_dynamic_name_does_not_count_the_topic_literal_itself(tmp_path: Path) -> None:
    _write(tmp_path, "backend/q/orders/orders.sql")
    _write(tmp_path, "backend/repo.py", 'load_sql("orders", name)\n')
    assert _check(tmp_path) == ["backend/q/orders/orders.sql"]


def test_path_literal_must_match_whole_trailing_segments(tmp_path: Path) -> None:
    _write(tmp_path, "backend/xsql/a.sql")
    _write(tmp_path, "backend/sql/a.sql")
    _write(tmp_path, "backend/sql/init.sql")
    _write(tmp_path, "backend/sql/reinit.sql")
    _write(tmp_path, "backend/use.py", 'a = open("backend/sql/a.sql")\nb = open("./sql/init.sql")\nc = base + "/reinit.sql"\n')
    assert _check(tmp_path) == ["backend/sql/reinit.sql", "backend/xsql/a.sql"]


def test_windows_separators_in_path_literals(tmp_path: Path) -> None:
    _write(tmp_path, "backend/sql/a/b.sql")
    _write(tmp_path, "backend/use.py", 'p = "backend\\\\sql\\\\a\\\\b.sql"\n')
    assert _check(tmp_path) == []


def test_bare_filename_in_unrelated_file_does_not_count_for_top_level_sql_folder(tmp_path: Path) -> None:
    _write(tmp_path, "sql/a.sql")
    _write(tmp_path, "tests/t.py", 'x = "a.sql"\n')
    assert _check(tmp_path, sql_root="sql") == ["sql/a.sql"]


def test_unparseable_python_file_is_reported_not_silently_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "backend/q/t/a.sql")
    _write(tmp_path, "backend/broken.py", 'load_sql("t", "a")\ndef (:\n')
    findings = check_unreferenced_sql_files(
        repo_root=tmp_path, sql_files=[tmp_path / "backend/q/t/a.sql"], python_files=sorted(tmp_path.rglob("*.py"))
    )
    assert sorted(f.rule for f in findings) == ["sql-check-python-unparseable", "sql-file-unreferenced"]


def _project(root: Path, config: str) -> None:
    _write(root, "backend/q/t/a.sql")
    _write(root, "backend/q/t/dead.sql")
    _write(root, "backend/repo.py", 'load_sql("t", "a")\n')
    _write(root, "backend/other.py", "x = 1\n")
    _write(root, "pyproject.toml", f"[project]\nname='x'\n[tool.bdt.dead_code]\n{config}\n")


def _run(root: Path, **kwargs) -> list:
    return dead_code.run(load_bdt_table("dead_code", root), repo_root=root, **kwargs)


def test_sql_check_is_opt_in(tmp_path: Path) -> None:
    _project(tmp_path, "")
    with pytest.raises(ApiUsageError, match="nothing to check"):
        _run(tmp_path)


def test_sql_check_scans_whole_repo_for_references(tmp_path: Path) -> None:
    _project(tmp_path, "sql_roots=['backend/q']")
    # the reference to a.sql lives in repo.py, not in the file that happens to be changed
    findings = _run(tmp_path)
    assert [f.path.name for f in findings] == ["dead.sql"]
    assert findings[0].rule == "sql-file-unreferenced"


def test_sql_check_reads_loader_and_ignore_config_from_pyproject(tmp_path: Path) -> None:
    _project(tmp_path, "sql_roots=['backend/q']\nsql_loader_functions=['get_query']\nsql_unreferenced_ignore=['backend/q/t/*.sql']")
    assert _run(tmp_path) == []  # everything ignored
    _project(tmp_path, "sql_roots=['backend/q']\nsql_loader_functions=['get_query']")
    # load_sql is no longer the loader name, so both files are unreferenced
    assert sorted(f.path.name for f in _run(tmp_path)) == ["a.sql", "dead.sql"]


def test_sql_check_accepts_bare_string_for_list_settings(tmp_path: Path) -> None:
    _project(tmp_path, "sql_roots='backend/q'")
    assert [f.path.name for f in _run(tmp_path)] == ["dead.sql"]


def test_sql_check_reports_missing_or_escaping_sql_root(tmp_path: Path) -> None:
    _project(tmp_path, "sql_roots=['nope', '../outside']")
    assert [f.rule for f in _run(tmp_path)] == ["lint-path-not-found", "lint-path-not-found"]


def test_sql_check_honours_exclude_dirs(tmp_path: Path) -> None:
    _project(tmp_path, "sql_roots=['backend/q']\nexclude_dirs=['t']")
    assert _run(tmp_path) == []  # the only SQL files sit in an excluded subdirectory


def test_only_narrows_the_run_and_rejects_unknown_checks(tmp_path: Path) -> None:
    _project(tmp_path, "sql_roots=['backend/q']")
    assert len(_run(tmp_path, only=["sql"])) == 1
    with pytest.raises(ApiUsageError, match=r"apps"):
        _run(tmp_path, only=["routes"])
    with pytest.raises(ApiUsageError, match="unknown check"):
        _run(tmp_path, only=["nope"])
    with pytest.raises(ApiUsageError, match="only applies to"):
        _run(tmp_path, only=["sql"], update_baseline=True)
