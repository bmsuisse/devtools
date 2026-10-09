from pathlib import Path

from bmsdna.devtools.lint_sql import check_sql_file


def _findings(source: str, path: Path) -> list:
    path.write_text(source)
    return check_sql_file(path)


def _rules(findings: list) -> set[str]:
    return {f.rule for f in findings}


def test_trivial_named_param_query_is_clean(tmp_path: Path) -> None:
    # Real pattern from OneSales' CustomerRepository.customer_exists.
    source = '''
async def customer_exists(cur, customer_id):
    await cur.execute("select 1 from dim.customer where id = %(id)s limit 1", {"id": customer_id})
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_complex_inline_query_bound_to_variable_flags_too_complex(tmp_path: Path) -> None:
    # Real pattern from OneSales' CustomerRepository.get_contact_proposals: a multi-line CTE
    # query assigned to a local var, then passed to .execute() by name.
    source = '''
async def get_contact_proposals(cur, customer_id):
    query = """
        with agent_ids as (
            select (sa).sales_agent_id as sales_agent_id
            from dim.customer c, unnest(c.sales_agents) as sa
            where c.id = %(customer_id)s
        ),
        agents as (
            select sa_dim.email as email, sa_dim.name as label, 'agent' as kind
            from agent_ids ai
            join dim.sales_agent sa_dim on sa_dim.id = ai.sales_agent_id
        )
        select email, label, kind from agents
    """
    await cur.execute(query, {"customer_id": customer_id})
'''
    findings = _findings(source, tmp_path / "a.py")
    assert _rules(findings) == {"sql-inline-too-complex"}


def test_join_without_cte_but_over_four_lines_flags_too_complex(tmp_path: Path) -> None:
    source = '''
async def f(cur):
    await cur.execute(
        "select c.id, c.name "
        "from dim.customer c "
        "join dim.sales_agent sa on sa.id = c.sales_agent_id "
        "where c.active = true"
    )
'''
    findings = _findings(source, tmp_path / "a.py")
    assert "sql-inline-too-complex" in _rules(findings)


def test_fstring_query_flags_injection(tmp_path: Path) -> None:
    # Real pattern from OneSales' offer_followup_status.py (ruff's S608 is suppressed there).
    source = '''
async def f(cur, table, column, value):
    await cur.execute(f"SELECT 1 FROM dim.{table} WHERE {column} = %(value)s", {"value": value})
'''
    findings = _findings(source, tmp_path / "a.py")
    assert _rules(findings) == {"sql-fstring-injection"}


def test_string_concat_query_flags_injection(tmp_path: Path) -> None:
    source = '''
async def f(cur, value):
    bad = "select * from t where id = " + str(value)
    await cur.execute(bad)
'''
    findings = _findings(source, tmp_path / "a.py")
    assert _rules(findings) == {"sql-concat-injection"}


def test_percent_format_query_flags_injection(tmp_path: Path) -> None:
    # The canonical vulnerable pattern (also flagged by ruff's S608).
    source = '''
def f(cur, identifier):
    query = "DELETE FROM foo WHERE id = '%s'" % identifier
    cur.execute(query)
'''
    findings = _findings(source, tmp_path / "a.py")
    assert _rules(findings) == {"sql-percent-format-injection"}


def test_str_format_query_flags_injection(tmp_path: Path) -> None:
    source = '''
def f(cur, column):
    query = "select {col} from users".format(col=column)
    cur.execute(query)
'''
    findings = _findings(source, tmp_path / "a.py")
    assert _rules(findings) == {"sql-format-injection"}


def test_positional_param_is_flagged(tmp_path: Path) -> None:
    source = '''
async def f(cur):
    await cur.execute("select * from t where id = %s", (1,))
'''
    findings = _findings(source, tmp_path / "a.py")
    assert _rules(findings) == {"sql-positional-param"}


def test_forbidden_lateral_join_is_flagged(tmp_path: Path) -> None:
    source = '''
async def f(cur):
    await cur.execute(
        "select a.id, x.val from a join lateral "
        "(select 1 as val from b where b.a_id = a.id) as x on true"
    )
'''
    findings = _findings(source, tmp_path / "a.py")
    assert "sql-forbidden-join" in _rules(findings)


def test_load_sql_call_is_not_flagged(tmp_path: Path) -> None:
    source = '''
async def f(cur, limit):
    await cur.execute(load_sql("users", "list_active_users"), {"limit": limit})
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_sql_loader_bound_method_is_not_flagged(tmp_path: Path) -> None:
    # pgdevkit's `SqlLoader(...).load_sql(...)` convention.
    source = '''
async def f(cur, limit):
    await cur.execute(sql_loader.load_sql("users", "list_active_users"), {"limit": limit})
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_tstring_query_is_not_flagged(tmp_path: Path) -> None:
    source = '''
async def f(cur, user_id):
    q = t"select * from users where id = {user_id}"
    await cur.execute(q)
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_sql_composed_call_is_not_flagged(tmp_path: Path) -> None:
    source = '''
from psycopg import sql

def f(cur, column):
    query = sql.SQL("select {col} from users where active = %(active)s").format(col=sql.Identifier(column))
    cur.execute(query, {"active": True})
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_unresolved_argument_is_not_flagged(tmp_path: Path) -> None:
    # `query` comes from a function parameter -- can't be resolved statically, must stay quiet.
    source = '''
async def f(cur, query, params):
    await cur.execute(query, params)
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_non_sql_execute_call_is_not_flagged(tmp_path: Path) -> None:
    # A duckdb COPY export (real pattern from OneSales' excel_export.py) -- parses to
    # exp.Copy, deliberately outside the accepted DML/query statement types.
    source = '''
def f(con, tmp, sheet_name):
    con.execute(f"COPY _exp TO '{tmp}' (FORMAT XLSX, HEADER true, SHEET '{sheet_name}')")
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_unrelated_execute_call_is_not_flagged(tmp_path: Path) -> None:
    source = '''
def f(runner):
    runner.execute("just a plain non-sql string, not a database call at all")
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_unsafe_branch_shadowed_by_later_safe_reassignment_is_still_flagged(tmp_path: Path) -> None:
    # An earlier branch assigns an injectable query; a later (unconditional-looking, but
    # actually just a different branch) reassignment looks safe. Only checking the lexically
    # last assignment would miss the unsafe branch entirely.
    source = '''
async def f(cur, cond, value):
    query = "select * from t where id = " + str(value)
    if cond:
        query = "select * from t where id = %(id)s"
    await cur.execute(query, {"id": value})
'''
    findings = _findings(source, tmp_path / "a.py")
    assert "sql-concat-injection" in _rules(findings)


def test_non_utf8_file_is_skipped_not_crashed(tmp_path: Path) -> None:
    path = tmp_path / "bad_encoding.py"
    path.write_bytes(b"\xff\xfe# not valid utf-8\n")
    assert check_sql_file(path) == []


def test_scopes_do_not_leak_between_functions(tmp_path: Path) -> None:
    # `query` in g() must not resolve to f()'s binding of the same name.
    source = '''
async def f(cur):
    query = "select * from t where id = " + "1"
    await cur.execute(query)

async def g(cur, query, params):
    await cur.execute(query, params)
'''
    findings = _findings(source, tmp_path / "a.py")
    assert len(findings) == 1
    assert findings[0].line == 4


def test_syntax_error_file_is_skipped(tmp_path: Path) -> None:
    assert check_sql_file(tmp_path / "bad.py", source="def f(:\n") == []


def test_sqlglot_and_literalstring_queries_are_trusted(tmp_path: Path) -> None:
    source = '''
from typing import LiteralString, cast
import sqlglot

def build() -> LiteralString:
    return "select 1"

def run(cur, expr, user_input):
    cur.execute(expr.sql(dialect="postgres"))
    cur.execute(sqlglot.select("a").from_("t").sql())
    cur.execute(cast(LiteralString, expr.sql()))
    cur.execute(build())
    q = expr.sql()
    cur.execute(q)
'''
    path = tmp_path / "a.py"
    path.write_text(source)
    assert check_sql_file(path, review=True) == []


def test_unverified_call_is_only_reported_in_review_mode(tmp_path: Path) -> None:
    source = '''
def run(cur, user_input):
    cur.execute(make_query(user_input))
'''
    path = tmp_path / "a.py"
    path.write_text(source)
    assert check_sql_file(path) == []
    findings = check_sql_file(path, review=True)
    assert [(f.rule, f.severity) for f in findings] == [("sql-unverified-call", "review")]


def test_dedent_and_strip_of_literal_are_unwrapped(tmp_path: Path) -> None:
    source = '''
import textwrap

def run(cur):
    cur.execute(textwrap.dedent("select 1 from t"), {})
    cur.execute("select 1 from t".strip())
'''
    path = tmp_path / "a.py"
    path.write_text(source)
    assert check_sql_file(path, review=True) == []


def test_sql_call_is_not_trusted_without_a_sqlglot_import(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("def run(cur, builder):\n    cur.execute(builder.sql())\n    cur.execute(exp.text)\n")
    assert [f.rule for f in check_sql_file(path, review=True)] == ["sql-unverified-call"]


def test_literalstring_cast_is_only_as_safe_as_its_argument(tmp_path: Path) -> None:
    source = '''
from typing import LiteralString, cast

def a(cur, user):
    cur.execute(cast(LiteralString, f"select * from t where a = {user}"))

def b(cur, user):
    cur.execute(cast(LiteralString, user))

def c(cur):
    cur.execute(cast(LiteralString, "select 1 from t"))
'''
    path = tmp_path / "a.py"
    path.write_text(source)
    assert sorted(f.rule for f in check_sql_file(path)) == ["sql-fstring-injection"]
    assert sorted(f.rule for f in check_sql_file(path, review=True)) == ["sql-fstring-injection", "sql-unverified-cast"]


def test_long_simple_insert_update_delete_allowed_inline(tmp_path: Path) -> None:
    source = '''
async def f(cur):
    await cur.execute(
        """
        insert into dim.customer (
            id,
            name,
            email,
            active
        ) values (%(id)s, %(name)s, %(email)s, true)
        on conflict (id) do update set name = excluded.name
        """,
        {},
    )
    await cur.execute(
        """
        update dim.customer
        set name = %(name)s,
            email = %(email)s,
            active = false
        where id = %(id)s
        """,
        {},
    )
    await cur.execute(
        """
        delete from dim.customer
        where id = %(id)s
          and active = false
          and email is null
        """,
        {},
    )
'''
    assert "sql-inline-too-complex" not in _rules(_findings(source, tmp_path / "a.py"))


def test_insert_select_and_update_from_still_too_complex(tmp_path: Path) -> None:
    source = '''
async def f(cur):
    await cur.execute("insert into a (id) select id from b")
    await cur.execute("update a set x = b.x from b where a.id = b.id")
    await cur.execute("delete from a using b where a.id = b.id")
'''
    findings = _findings(source, tmp_path / "a.py")
    assert [f.rule for f in findings].count("sql-inline-too-complex") == 3


_COMPLEX_SELECT = '''"""
select a.id, p.price
from core.dim_article a
left join core.dim_price p on p.article_id = a.id
where a.active
"""'''


def test_sql_named_variable_with_complex_literal_flagged_without_execute(tmp_path: Path) -> None:
    source = f"ARTICLES_SQL = {_COMPLEX_SELECT}\nother_sql: str = {_COMPLEX_SELECT}\nnot_a_query = {_COMPLEX_SELECT}\n"
    findings = _findings(source, tmp_path / "a.py")
    assert [(f.rule, f.line) for f in findings] == [("sql-inline-too-complex", 1), ("sql-inline-too-complex", 7)]


def test_sql_named_variable_simple_or_dml_not_flagged(tmp_path: Path) -> None:
    source = 'GET_SQL = "select id from t where id = %(id)s"\nINSERT_SQL = """\ninsert into t (\n a,\n b,\n c,\n d\n) values (1, 2, 3, 4)\n"""\n'
    assert _findings(source, tmp_path / "a.py") == []


def test_sql_named_variable_used_in_execute_reported_once(tmp_path: Path) -> None:
    source = f"ARTICLES_SQL = {_COMPLEX_SELECT}\n\ndef f(cur):\n    cur.execute(ARTICLES_SQL)\n"
    assert len(_findings(source, tmp_path / "a.py")) == 1


def test_sql_named_alias_of_complex_literal_still_flagged_at_execute(tmp_path: Path) -> None:
    source = f"""
async def f(cur):
    base = {_COMPLEX_SELECT}
    q_sql = base
    await cur.execute(q_sql)
"""
    assert "sql-inline-too-complex" in _rules(_findings(source, tmp_path / "a.py"))


def test_fetch_all_with_fstring_is_flagged_like_execute(tmp_path: Path) -> None:
    source = '''
from pgdevkit.db import fetch_all

async def f(table):
    return await fetch_all(f"SELECT id FROM dim.{table} WHERE active")
'''
    assert _rules(_findings(source, tmp_path / "a.py")) == {"sql-fstring-injection"}


def test_fetch_helpers_flag_concat_format_and_percent(tmp_path: Path) -> None:
    source = '''
async def f(db, value):
    await fetch_one("select * from t where id = " + str(value))
    await fetch_scalar("select count(*) from t where id = %s" % value)
    await db.fetch_all("select * from t where id = {}".format(value))
'''
    assert _rules(_findings(source, tmp_path / "a.py")) == {
        "sql-concat-injection",
        "sql-percent-format-injection",
        "sql-format-injection",
    }


def test_fetch_all_accepts_literal_load_sql_sqlglot_and_template(tmp_path: Path) -> None:
    source = '''
from sqlglot import select
from pgdevkit.db import fetch_all

async def f(sql_loader, model, user_id):
    await fetch_all(sql_loader.load_sql("users", "list_active"), {"limit": 5}, model=model)
    await fetch_all(select("id").from_("t").sql(dialect="postgres"))
    await fetch_all(t"select id from t where user_id = {user_id}")
    await fetch_all("select id from t where user_id = %(id)s", {"id": user_id}, model=model)
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_fetch_all_applies_inline_complexity_and_positional_rules(tmp_path: Path) -> None:
    source = '''
async def f(model):
    await fetch_all("select a.id from a join b on b.a_id = a.id", model=model)
    await fetch_all("select id from t where x = %s", (1,))
'''
    assert _rules(_findings(source, tmp_path / "a.py")) == {"sql-inline-too-complex", "sql-positional-param"}


def test_unrelated_fetch_all_name_with_non_sql_text_is_not_flagged(tmp_path: Path) -> None:
    source = '''
async def f(client, name):
    return await client.fetch_all(f"users/{name}")
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_sqlglot_builder_with_interpolated_string_is_flagged(tmp_path: Path) -> None:
    source = '''
from sqlglot import select
import sqlglot

async def f(con, user_filter, col):
    q1 = select("id").from_("t").where(f"name = {user_filter}")
    q2 = sqlglot.parse_one("select " + col + " from t")
    q3 = select("id").from_("t").order_by("{} desc".format(col))
    await fetch_all(q1.sql(dialect="postgres"))
'''
    findings = _findings(source, tmp_path / "a.py")
    assert _rules(findings) == {"sql-sqlglot-string-injection"}
    assert len(findings) == 3


def test_sqlglot_builders_with_literals_and_nodes_are_clean(tmp_path: Path) -> None:
    source = '''
from sqlglot import exp, select

async def f(col):
    q = select("id", "name").from_("dim.customer").where("active = true")
    q = q.where(exp.column(col).eq(exp.Placeholder(this="id")))
    q = q.order_by(exp.to_identifier(col).sql())
    return q
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_sqlglot_rule_ignores_files_that_do_not_use_sqlglot(tmp_path: Path) -> None:
    source = '''
def f(qs, name):
    return qs.where(f"name = {name}").select(f"x{name}")
'''
    assert _findings(source, tmp_path / "a.py") == []


def test_sqlglot_method_on_a_variable_is_flagged_but_generic_join_is_not(tmp_path: Path) -> None:
    source = '''
from sqlglot import select

def f(user_filter, parts):
    q = select("id").from_("t")
    q = q.where(f"name = {user_filter}")
    label = ", ".join(f"{p}!" for p in parts) + "".join(f"{p}" for p in parts)
    return q, label
'''
    findings = _findings(source, tmp_path / "a.py")
    assert [f.rule for f in findings] == ["sql-sqlglot-string-injection"]
