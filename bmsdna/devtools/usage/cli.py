"""`bdt usage`: profile Claude Code / codex session logs per step of the dev-workflow."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import typer

from . import pricing

usage_app = typer.Typer(
    name="usage",
    help="Where do the tokens of Claude Code / codex sessions go, per step of the dev-workflow? "
    "(needs the `usage` extra: `uv tool install 'bmsdna-devtools\\[usage]'`)",
)

INSTALL_HINT = "bdt usage needs duckdb and plotly: install the extra, e.g. `uv tool install --reinstall 'bmsdna-devtools[usage]'`"
DEFAULT_CLAUDE_LOGS = "~/.claude/projects"
DEFAULT_CODEX_LOGS = "~/.codex/sessions"


def default_data_dir() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()
    return cache / "bdt" / "usage"


def require_duckdb():
    try:
        import duckdb
    except ImportError:
        print(INSTALL_HINT, file=sys.stderr)
        raise typer.Exit(1) from None
    return duckdb


@usage_app.command("analyze")
def analyze(
    logs: str = typer.Option(DEFAULT_CLAUDE_LOGS, "--logs", help="Claude Code projects directory"),
    codex_logs: str = typer.Option(DEFAULT_CODEX_LOGS, "--codex-logs", help="codex sessions directory (skipped if it does not exist)"),
    no_codex: bool = typer.Option(False, "--no-codex", help="Ignore codex logs"),
    db: str | None = typer.Option(None, "--db", help="DuckDB file (default: <cache dir>/bdt/usage/profile.duckdb). Doubles as the parse cache"),
    out: str | None = typer.Option(None, "--out", help="Directory for report.html and the CSV / Parquet exports (default: next to the database)"),
    scope: str = typer.Option("workflow", "--scope", help="workflow: only threads that follow dev-workflow (and what they spawn); all: every session"),
    since: str = typer.Option("1970-01-01", "--since", help="Only messages from this date on (YYYY-MM-DD)"),
    rebuild: bool = typer.Option(False, "--rebuild", help="Ignore the parse cache in the database and re-read every log file"),
    no_report: bool = typer.Option(False, "--no-report", help="Only print the summary"),
    price: list[str] = typer.Option(
        [], "--price", metavar="MODEL=IN,OUT,CACHED",
        help="USD per million tokens for a model without a built-in price (codex / OpenAI models), repeatable. "
        "Example: --price gpt-6-sol=1.25,10,0.125",
    ),
) -> None:
    """Load the session logs, attribute every model turn to a workflow step, price it, and print / render the result.

    The first run parses every log file; later runs only parse new or changed ones. Costs are API list prices
    (not your actual bill); turns of a model without a price are counted in tokens but not in cost.
    """
    if scope not in ("workflow", "all"):
        raise typer.BadParameter("Must be 'workflow' or 'all'", param_hint="--scope")
    try:
        date.fromisoformat(since)
    except ValueError:
        raise typer.BadParameter("Must be a date like 2026-10-01", param_hint="--since") from None
    try:
        prices = dict(pricing.parse_price(p) for p in price)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--price") from None
    duckdb = require_duckdb()
    from . import pipeline

    data_dir = default_data_dir()
    db_path = Path(db).expanduser() if db else data_dir / "profile.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    print(f"Reading {logs}" + ("" if no_codex else f" and {codex_logs}") + " ...", file=sys.stderr)
    stats = pipeline.build(
        con, Path(logs), None if no_codex else Path(codex_logs), scope=scope, since=since, rebuild=rebuild, prices=prices
    )
    print(f"{stats.files} log files, {stats.ingested} parsed ({stats.files - stats.ingested} from cache), {stats.removed} removed", file=sys.stderr)

    summary = con.execute("SELECT * FROM v_summary").fetchone()
    if not summary or not summary[2]:
        print("No sessions in scope. Nothing used dev-workflow in these logs (try --scope all)?", file=sys.stderr)
        raise typer.Exit(1)
    con.sql("SELECT * EXCLUDE (unpriced_msgs, unpriced_models) FROM v_summary").show(max_width=200)
    con.sql("""
        SELECT label AS step, msgs AS turns, sessions, round(cost, 2) AS usd,
               round(pct_cost, 1) AS "% cost", round(pct_tokens, 1) AS "% tokens",
               round(pct_fresh_tokens, 1) AS "% fresh tokens",
               round(pct_of_step_via_subagents) AS "% via subagents"
        FROM v_steps""").show(max_width=200)
    con.sql("SELECT provider, msgs AS turns, tokens, round(cost, 2) AS usd, round(pct_tokens, 1) AS \"% tokens\" FROM v_providers").show(max_width=200)
    unpriced = con.execute("SELECT unpriced_msgs, unpriced_models FROM v_summary").fetchone()
    if unpriced and unpriced[0]:
        print(
            f"Note: {unpriced[0]} turns of {unpriced[1]} have no price, so they are missing from the cost columns. "
            "Add one with --price MODEL=IN,OUT,CACHED (USD per million tokens).",
            file=sys.stderr,
        )
    if not no_report:
        from . import report

        out_dir = Path(out).expanduser() if out else db_path.parent
        print(f"Report: {report.write_report(con, out_dir)}", file=sys.stderr)


@usage_app.command("sql")
def sql(
    query: str = typer.Argument(..., help="SQL to run against a database built by `bdt usage analyze` (tables: facts, msgs, threads, v_*)"),
    db: str | None = typer.Option(None, "--db", help="DuckDB file (default: the one `analyze` writes)"),
) -> None:
    """Run ad-hoc SQL on the last analysis, e.g. `bdt usage sql "select label, round(pct_cost, 1) from v_steps"`."""
    duckdb = require_duckdb()
    db_path = Path(db).expanduser() if db else default_data_dir() / "profile.duckdb"
    if not db_path.exists():
        print(f"{db_path} does not exist yet: run `bdt usage analyze` first.", file=sys.stderr)
        raise typer.Exit(1)
    con = duckdb.connect(str(db_path), read_only=True)
    con.sql(query).show(max_width=200, max_rows=100)
