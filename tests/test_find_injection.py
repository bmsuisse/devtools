import subprocess
from pathlib import Path

import pytest

from bmsdna.devtools import find_injection
from bmsdna.devtools.injection_frontend import check_frontend_file
from bmsdna.devtools.injection_python import check_python_sinks


def _py(tmp_path: Path, source: str) -> list[tuple[str, str]]:
    path = tmp_path / "m.py"
    path.write_text(source)
    return [(f.rule, f.severity) for f in check_python_sinks(path)]


def _fe(tmp_path: Path, source: str, name: str = "c.tsx") -> set[str]:
    path = tmp_path / name
    path.write_text(source)
    return {f.rule for f in check_frontend_file(path)}


def test_python_eval_and_shell(tmp_path: Path) -> None:
    src = """
import os, subprocess
eval(user)
eval("1+1")
os.system("ls " + name)
subprocess.run(cmd, shell=True)
subprocess.run("ls", shell=True)
subprocess.run(["ls", name])
"""
    assert _py(tmp_path, src) == [
        ("py-eval-exec", "error"),
        ("py-shell-command", "error"),
        ("py-shell-command", "error"),
        ("py-shell-true", "review"),
    ]


def test_python_yaml_pickle_markup_jinja(tmp_path: Path) -> None:
    src = """
import yaml, pickle
from markupsafe import Markup
yaml.load(data)
yaml.load(data, Loader=yaml.SafeLoader)
yaml.safe_load(data)
pickle.loads(blob)
Markup(user_html)
Markup("<b>x</b>")
Environment(autoescape=False)
render_template_string(request_arg)
"""
    assert _py(tmp_path, src) == [
        ("py-unsafe-yaml", "error"),
        ("py-unsafe-deserialization", "review"),
        ("py-unescaped-markup", "review"),
        ("py-autoescape-off", "error"),
        ("py-template-injection", "error"),
    ]


def test_frontend_dom_sinks(tmp_path: Path) -> None:
    src = """
export function A({ html }) {
  el.innerHTML = html;
  el.insertAdjacentHTML("beforeend", html);
  document.write(html);
  eval(code);
  new Function("a", code);
  setTimeout("run()", 10);
  win.postMessage(data, "*");
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}
"""
    assert _fe(tmp_path, src) == {
        "fe-inner-html",
        "fe-document-write",
        "fe-eval",
        "fe-new-function",
        "fe-timer-string",
        "fe-post-message-star",
        "fe-dangerously-set-inner-html",
    }


def test_frontend_ignores_comments_and_strings(tmp_path: Path) -> None:
    src = """
// el.innerHTML = x; eval(y)
const s = "eval(x) and document.write(y)";
const ok = el.textContent === 'a';
setTimeout(() => run(), 10);
"""
    assert _fe(tmp_path, src) == set()


def test_iframe_sandbox_rules(tmp_path: Path) -> None:
    assert _fe(tmp_path, '<iframe src="/x" />') == {"fe-iframe-no-sandbox"}
    assert _fe(tmp_path, '<iframe src="/x" sandbox />') == set()
    assert _fe(tmp_path, '<iframe src="/x" sandbox="allow-forms" />') == set()
    assert _fe(tmp_path, "<iframe onLoad={() => a > b} />") == {"fe-iframe-no-sandbox"}
    assert _fe(tmp_path, '<iframe sandbox="allow-scripts allow-same-origin" />') == {"fe-iframe-sandbox-escape"}
    assert _fe(tmp_path, '<iframe src="https://x.test/a"></iframe>', "i.html") == {"fe-iframe-no-sandbox"}


def test_vue_v_html_and_javascript_url(tmp_path: Path) -> None:
    src = '<template><div v-html="x"></div><a href="javascript:void(0)">x</a></template>'
    assert _fe(tmp_path, src, "a.vue") == {"fe-v-html", "fe-javascript-url"}


def _run(tmp_path: Path, **kwargs):
    return find_injection.run([str(tmp_path)], root=tmp_path, **kwargs)


def test_csp_missing_for_web_project(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    assert [f.rule for f in _run(tmp_path).findings] == ["csp-missing"]
    (tmp_path / "web.config").write_text('<add name="Content-Security-Policy" value="default-src \'self\'" />')
    assert _run(tmp_path).findings == []


def test_csp_not_required_for_non_web_project(tmp_path: Path) -> None:
    (tmp_path / "lib.py").write_text("def f():\n    return 1\n")
    assert _run(tmp_path).findings == []


def test_csp_weakened_is_reported(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<meta http-equiv=\"Content-Security-Policy\" content=\"script-src 'self' 'unsafe-inline'\"><p>hi</p>")
    assert [f.rule for f in _run(tmp_path).findings] == ["csp-weakened"]


def test_pragma_ignore_and_severity_exit_codes(tmp_path: Path) -> None:
    (tmp_path / "web.config").write_text("Content-Security-Policy: default-src 'self'")
    (tmp_path / "a.ts").write_text("el.innerHTML = x;\n")
    result = _run(tmp_path)
    assert [f.rule for f in result.reviews] == ["fe-inner-html"]
    assert find_injection.print_report(result) == 0
    assert find_injection.print_report(result, strict=True) == 1
    (tmp_path / "a.ts").write_text("// bdt-lint: ignore fe-inner-html -- static markup\nel.innerHTML = x;\n")
    assert _run(tmp_path).findings == []
    (tmp_path / "a.ts").write_text("eval(x);\n")
    assert find_injection.print_report(_run(tmp_path)) == 1


def test_sql_injection_is_error_and_unverified_call_is_review(tmp_path: Path) -> None:
    (tmp_path / "db.py").write_text(
        'def a(cur, x):\n    cur.execute(f"select * from t where a = {x}")\n\ndef b(cur, x):\n    cur.execute(build(x))\n\n'
    )
    result = _run(tmp_path)
    assert [f.rule for f in result.errors] == ["sql-fstring-injection"]
    assert [f.rule for f in result.reviews] == ["sql-unverified-call"]


def test_pgdevkit_execute_is_a_sql_sink_for_find_injection(tmp_path: Path) -> None:
    (tmp_path / "db.py").write_text(
        "from pgdevkit.db import execute\n\n"
        'async def a(x):\n    await execute(f"update t set a = {x}")\n\n'
        "async def b(x):\n    await execute(build(x))\n"
    )
    (tmp_path / "other.py").write_text('from lib import execute\n\ndef a(x):\n    execute(f"update t set a = {x}")\n')
    result = _run(tmp_path)
    assert [(f.path.name, f.rule) for f in result.errors] == [("db.py", "sql-fstring-injection")]
    assert [(f.path.name, f.rule) for f in result.reviews] == [("db.py", "sql-unverified-call")]


def test_tests_and_missing_paths(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("eval(x)\n")
    assert _run(tmp_path).findings == []
    result = find_injection.run([str(tmp_path / "nope")], root=tmp_path)
    assert [f.rule for f in result.findings] == ["path-not-found"]


def test_diff_mode_scans_changed_and_untracked_files(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-b", "main")
    (tmp_path / "old.py").write_text("eval(x)\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("checkout", "-b", "feature")
    (tmp_path / "new.py").write_text("exec(y)\n")
    git("add", ".")
    git("commit", "-m", "change")
    (tmp_path / "untracked.py").write_text("eval(z)\n")
    result = find_injection.run([], root=tmp_path, diff=True, base="main")
    assert sorted(f.path.name for f in result.findings) == ["new.py", "untracked.py"]


def test_diff_mode_without_base_branch_exits(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "odd"], cwd=tmp_path, check=True, capture_output=True)
    with pytest.raises(SystemExit):
        find_injection.run([], root=tmp_path, diff=True)


def test_python_aliased_imports_and_keyword_args(tmp_path: Path) -> None:
    src = """
from os import system
from subprocess import run as sp_run
import subprocess as sp
from yaml import load
system(cmd)
sp_run(cmd, shell=True)
sp.run(args=cmd, shell=True)
os_cmd = system("a" + "b")
load(data)
"""
    assert [r for r, _ in _py(tmp_path, src)] == [
        "py-shell-command",
        "py-shell-command",
        "py-shell-command",
        "py-unsafe-yaml",
    ]


def test_frontend_regex_edge_cases(tmp_path: Path) -> None:
    assert "fe-post-message-star" in _fe(tmp_path, "win.postMessage(JSON.stringify(payload), '*');", "a.ts")
    assert _fe(tmp_path, '<iframe src="https://host/sandbox/embed" data-sandbox="x" />') == {"fe-iframe-no-sandbox"}
    assert _fe(tmp_path, "<p>JavaScript: disabled</p><!-- javascript: x -->", "a.html") == set()
    assert _fe(tmp_path, '<a href="javascript:alert(1)">x</a>', "a.html") == {"fe-javascript-url"}


def test_csp_wildcard_after_quoted_source(tmp_path: Path) -> None:
    (tmp_path / "staticwebapp.config.json").write_text('{"globalHeaders": {"Content-Security-Policy": "script-src \'self\' *; img-src *"}}')
    (tmp_path / "app.py").write_text("from fastapi import FastAPI\n")
    result = _run(tmp_path)
    assert [f.rule for f in result.findings] == ["csp-weakened"]
    assert result.findings[0].path.name == "staticwebapp.config.json"


def test_relative_paths_still_skip_test_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "helpers.py").write_text("eval(x)\n")
    monkeypatch.chdir(tmp_path)
    assert find_injection.run(["."], root=tmp_path).findings == []


def test_diff_mode_from_subdirectory_finds_untracked_files(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-b", "main")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "keep.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-m", "base")
    (tmp_path / "backend" / "café.py").write_text("eval(y)\n")
    result = find_injection.run([], root=tmp_path / "backend", diff=True, base="main")
    assert [f.path.name for f in result.findings] == ["café.py"]


def _rules_by_file(result) -> set[str]:
    return {f.path.name for f in result.findings}


def test_exclude_dir_name_path_and_glob(tmp_path: Path) -> None:
    for d in ("vendor", "lib/generated", "web"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "bad.py").write_text("eval(x)\n")
    (tmp_path / "web" / "bundle.js").write_text("eval(x);\n")
    (tmp_path / "web.config").write_text("Content-Security-Policy: default-src 'self'")
    assert len(find_injection.run([], root=tmp_path).findings) == 4
    assert len(find_injection.run([], root=tmp_path, exclude=["vendor"]).findings) == 3
    assert len(find_injection.run([], root=tmp_path, exclude=["lib/generated"]).findings) == 3
    assert len(find_injection.run([], root=tmp_path, exclude=["web/*.js", "vendor/"]).findings) == 2
    assert [f.path.name for f in find_injection.run([], root=tmp_path, exclude=["vendor", "lib", "web/*.js"]).findings] == ["bad.py"]


def test_exclude_dirs_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.lint]\nexclude_dirs = ["legacy"]\n')
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "bad.py").write_text("eval(x)\n")
    assert find_injection.run([], root=tmp_path).findings == []


def test_gitignored_files_are_skipped_unless_disabled(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("built/\n")
    (tmp_path / "built").mkdir()
    (tmp_path / "built" / "bad.py").write_text("eval(x)\n")
    (tmp_path / "src.py").write_text("eval(y)\n")
    assert [f.path.name for f in find_injection.run([], root=tmp_path).findings] == ["src.py"]
    assert len(find_injection.run([], root=tmp_path, respect_gitignore=False).findings) == 2


def test_min_js_is_skipped_long_lines_are_not_and_findings_deduplicated(tmp_path: Path) -> None:
    (tmp_path / "app.min.js").write_text("eval(x);")
    (tmp_path / "bundle.js").write_text("a=1;" * 400 + "eval(x);\n")
    (tmp_path / "dup.js").write_text("el.innerHTML = a; el.innerHTML = b;\n")
    (tmp_path / "web.config").write_text("Content-Security-Policy: default-src 'self'")
    result = find_injection.run([], root=tmp_path)
    assert [(f.path.name, f.rule) for f in result.findings] == [("bundle.js", "fe-eval"), ("dup.js", "fe-inner-html")]


def test_sqlglot_expression_sql_is_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        """import sqlglot
from sqlglot import exp, select
from typing import cast, LiteralString


def run(conn, t):
    expr = select("a").from_(t).where(exp.column("b").eq(1))
    conn.execute(expr.sql())
    conn.execute(sqlglot.parse_one("select 1").sql(dialect="postgres"))
    sql = exp.select("*").from_(exp.to_table(t)).sql("tsql")
    conn.execute(sql)
    conn.execute(cast(LiteralString, select("a").from_(t).sql(pretty=True)))
"""
    )
    assert find_injection.run([], root=tmp_path).findings == []


def test_subprocess_arg_list_with_shell_true_and_bare_sql(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text(
        """import subprocess
from psycopg.sql import SQL


def run(conn, target):
    subprocess.check_call(["bun", "run", "build"], shell=True)
    subprocess.check_call(["bun", "run", target], cwd=target, shell=True)
    conn.execute(SQL("UPDATE t SET a = 1 WHERE id = %s"), (1,))
"""
    )
    (tmp_path / "bad.py").write_text(
        """import subprocess


def run(cmd, target):
    subprocess.check_call([cmd, "x"], shell=True)
    subprocess.check_call(f"bun run {target}", shell=True)
"""
    )
    result = find_injection.run([], root=tmp_path)
    assert [(f.path.name, f.line, f.rule) for f in result.findings] == [("bad.py", 5, "py-shell-command"), ("bad.py", 6, "py-shell-command")]


def test_csp_weakening_in_test_files_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "app.js").write_text("run();\n")
    (tmp_path / "web.config").write_text("Content-Security-Policy: default-src 'self'")
    (tmp_path / "test_headers.py").write_text("H = \"Content-Security-Policy: script-src 'unsafe-eval'\"\n")
    assert find_injection.run([], root=tmp_path).findings == []


def _lines(tmp_path: Path, src: str) -> list[int]:
    path = tmp_path / "m.py"
    path.write_text(src)
    return [f.line for f in check_python_sinks(path)]


def test_names_assigned_only_constants_are_constant(tmp_path: Path) -> None:
    src = """import os, subprocess
GREETING = "echo hi"
CMD = GREETING + " there"


def ok(flag):
    os.system(CMD)
    cmd = "ls" if flag else "pwd"
    os.system(cmd)
    for c in ("a", "b"):
        os.system(c)
    os.system(f"{GREETING} now")
    prog = "bun"
    subprocess.check_call([prog, "x"], shell=True)


def bad(arg, flag):
    cmd = "ls"
    cmd += arg
    os.system(cmd)
    other = "ls"
    if flag:
        other = arg
    os.system(other)
    os.system(arg)
    for c in arg:
        os.system(c)
    os.system(undefined_name)
"""
    assert _lines(tmp_path, src) == [20, 24, 25, 27, 28]


def test_global_rebinding_and_class_scope_defeat_constness(tmp_path: Path) -> None:
    src = """import os
GREETING = "echo hi"


def rebind():
    global GREETING
    GREETING = input()


def use():
    os.system(GREETING)


class K:
    x = "ls"

    def m(self):
        os.system(x)
"""
    assert _lines(tmp_path, src) == [11, 18]
