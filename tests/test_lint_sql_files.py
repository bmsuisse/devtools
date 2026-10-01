from pathlib import Path

from bmsdna.devtools import lint as lint_mod
from bmsdna.devtools.lint_sql_files import check_unreferenced_sql_files

_EXCLUDE = frozenset({".venv", "node_modules"})


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _check(root: Path, **kwargs) -> list[str]:
    py_files = sorted(root.rglob("*.py"))
    findings = check_unreferenced_sql_files(
        repo_root=root,
        sql_roots=kwargs.pop("sql_roots", ["backend"]),
        python_files=py_files,
        exclude_dir_names=_EXCLUDE,
        **kwargs,
    )
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


def test_missing_root_is_reported_not_silently_ignored(tmp_path: Path) -> None:
    findings = check_unreferenced_sql_files(
        repo_root=tmp_path,
        sql_roots=["nope"],
        python_files=[],
        exclude_dir_names=_EXCLUDE,
    )
    assert [f.rule for f in findings] == ["lint-path-not-found"]


def test_unparseable_python_file_is_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "backend/q/t/a.sql")
    _write(tmp_path, "backend/broken.py", "def (:\n")
    _write(tmp_path, "backend/repo.py", 'load_sql("t", "a")\n')
    assert _check(tmp_path) == []


def test_lint_run_is_opt_in_and_scans_whole_repo_even_with_explicit_paths(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "backend/q/t/a.sql")
    _write(tmp_path, "backend/q/t/dead.sql")
    _write(tmp_path, "backend/repo.py", 'load_sql("t", "a")\n')
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    off = lint_mod.run([str(tmp_path / "backend/repo.py")], root=tmp_path, skip_tooling_check=True)
    assert off.ok

    _write(
        tmp_path,
        "pyproject.toml",
        "[project]\nname='x'\n[tool.bdt.lint]\nsql_roots=['backend/q']\n",
    )
    on = lint_mod.run([str(tmp_path / "backend/repo.py")], root=tmp_path, skip_tooling_check=True)
    assert [f.path.name for f in on.findings] == ["dead.sql"]
    assert on.findings[0].rule == "sql-file-unreferenced"
