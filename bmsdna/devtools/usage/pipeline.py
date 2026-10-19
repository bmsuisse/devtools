"""Build the usage database: load the session logs with DuckDB, assign workflow steps, derive views."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from . import pricing

if TYPE_CHECKING:
    import duckdb

REVIEW_PHASES = {"review_quick", "review_full"}
KIND_RANK = {"start": 0, "skill": 1, "review_agent": 2, "cmd": 3, "edit": 4, "soft": 5, "once": 6}

# Bump when the rec_* tables or their parsing change: caches with another version are rebuilt.
CACHE_VERSION = "2"
REC_TABLES = ("rec_assistant", "rec_images", "rec_sessions", "rec_cmds", "rec_threads")

# provider -> (ingest script, glob below the log directory)
PROVIDERS = {
    "claude": ("01_ingest_claude.sql", "*.jsonl"),
    "codex": ("01_ingest_codex.sql", "*.jsonl"),
}


@dataclass
class BuildStats:
    files: int
    ingested: int
    removed: int


def _sql(name: str, **subs: str) -> str:
    text = resources.files("bmsdna.devtools.usage").joinpath("sql", name).read_text(encoding="utf-8")
    for key, value in subs.items():
        text = text.replace(f"__{key}__", value)
    return text


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _sql_list(values: list[str]) -> str:
    return "[" + ", ".join(f"'{_quote(v)}'" for v in values) + "]"


def assign_phases(con: duckdb.DuckDBPyConnection, scope: str) -> None:
    """Walk each thread in time order and give every message a workflow phase.

    A thread that follows the workflow (invoked dev-workflow, or scope='all') starts in 'setup' and
    switches phase on the next marker; the phase stays "sticky" until the next one (test runs are the
    exception: they only count for the turn that runs them). Other spawned threads inherit the phase of
    the message that spawned them and only react to strong markers (review/design/plan skills).
    """
    msgs_by_thread: dict[str, list[tuple[str, datetime]]] = defaultdict(list)
    thread_of_mid: dict[str, str] = {}
    for tid, mid, ts in con.execute("SELECT thread_id, mid, ts FROM msgs ORDER BY thread_id, ts, mid").fetchall():
        msgs_by_thread[tid].append((mid, ts))
        thread_of_mid[mid] = tid

    markers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for mid, phase, kind in con.execute("SELECT mid, phase, kind FROM tool_markers ORDER BY tool_use_id").fetchall():
        markers[mid].append((phase, kind))

    meta = {r[0]: (r[1], r[2] or "") for r in con.execute("SELECT thread_id, parent_mid, agent_type FROM threads").fetchall()}

    start_ts: dict[str, datetime] = {}
    for tid, ts in con.execute(
        "SELECT m.thread_id, min(m.ts) FROM msgs m JOIN tool_markers k USING (mid) WHERE k.kind = 'start' GROUP BY 1"
    ).fetchall():
        start_ts[tid] = ts
    for tid, ts in con.execute("SELECT thread_id, ts FROM workflow_commands").fetchall():
        if tid in msgs_by_thread:
            start_ts[tid] = min(start_ts.get(tid, ts), ts)
    # Spawned threads that run the whole workflow themselves: told so in the spawn prompt (Claude), or seen doing it.
    for tid, ts in con.execute(
        """
        SELECT th.thread_id, min(m.ts)
        FROM threads th
        JOIN msgs m ON m.thread_id = th.thread_id
        WHERE th.thread_id IN (SELECT m2.thread_id FROM msgs m2 JOIN tool_markers k USING (mid) WHERE k.wf_signal)
           OR th.thread_id IN (
                SELECT am.thread_id
                FROM agent_meta am
                JOIN tool_uses tu ON tu.tool_use_id = am.parent_tool_use_id
                WHERE tu.name IN ('Agent', 'Task')
                  AND regexp_matches(lower(json_extract_string(tu.input, '$.prompt')),
                                     'follow (the )?[`~/.\\w-]{0,40}dev-workflow|follow the workflow')
                  AND coalesce(json_extract_string(tu.input, '$.subagent_type'), '') NOT LIKE '%bms-review-%')
        GROUP BY 1
        """
    ).fetchall():
        start_ts.setdefault(tid, ts)
    if scope == "all":
        for tid, items in msgs_by_thread.items():
            if tid not in meta:
                start_ts[tid] = items[0][1]

    phase_of: dict[str, str] = {}
    sticky_of: dict[str, str] = {}  # the phase a spawned thread inherits
    pending = set(msgs_by_thread)

    def run_thread(tid: str, inherited: str) -> None:
        agent_type = meta.get(tid, (None, ""))[1]
        if "bms-review-" in agent_type and inherited not in REVIEW_PHASES and inherited != "outside":
            inherited = "review_full"
        phase, started, begin = inherited, False, start_ts.get(tid)
        for mid, ts in msgs_by_thread[tid]:
            if begin is not None and not started and ts >= begin:
                phase, started = "setup", True
            once = None
            for ph, kind in sorted(markers.get(mid, []), key=lambda m: KIND_RANK[m[1]]):
                if kind == "skill" and phase != "outside":
                    phase = ph
                elif kind == "review_agent" and phase != "outside" and phase not in REVIEW_PHASES:
                    phase = ph
                elif kind in ("cmd", "edit") and started:
                    phase = ph
                elif kind == "soft" and started and phase != "design_review":
                    phase = ph
                elif kind == "once" and started:
                    once = ph
            phase_of[mid] = once or phase
            sticky_of[mid] = phase

    while pending:
        progressed = False
        for tid in sorted(pending):
            parent_mid = meta.get(tid, (None, None))[0]
            parent_thread = thread_of_mid.get(parent_mid) if parent_mid else None
            if parent_thread in pending and parent_thread != tid:
                continue
            inherited = sticky_of.get(parent_mid, "outside") if parent_mid else "outside"
            run_thread(tid, inherited)
            pending.discard(tid)
            progressed = True
        if not progressed:
            for tid in sorted(pending):
                run_thread(tid, "outside")
            break

    con.execute("CREATE OR REPLACE TABLE msg_phase (mid VARCHAR, phase VARCHAR)")
    con.execute("INSERT INTO msg_phase SELECT unnest(?), unnest(?)", [list(phase_of), list(phase_of.values())])


def ingest(
    con: duckdb.DuckDBPyConnection, sources: dict[str, Path], rebuild: bool = False
) -> BuildStats:
    """Bring the rec_* cache tables up to date, parsing only new or changed log files.

    `sources` maps a provider ("claude", "codex") to the directory holding its logs; a provider that is not
    listed is left alone (its cached rows stay, they are just not analysed, see `build`).
    """
    con.execute(_sql("00_schema.sql"))
    row = con.execute("SELECT value FROM cache_meta WHERE key = 'version'").fetchone()
    if rebuild or (row and row[0] != CACHE_VERSION):
        for table in (*REC_TABLES, "src_files"):
            con.execute(f"DELETE FROM {table}")
    con.execute("DELETE FROM cache_meta WHERE key = 'version'")
    con.execute("INSERT INTO cache_meta VALUES ('version', ?)", [CACHE_VERSION])

    files = ingested = removed = 0
    for provider, root in sources.items():
        script, pattern = PROVIDERS[provider]
        on_disk: dict[str, tuple[int, int]] = {}
        for path in root.rglob(pattern):
            st = path.stat()
            on_disk[str(path)] = (st.st_size, st.st_mtime_ns)
        prefix = str(root) + "/"
        known = {
            f: (size, mtime)
            for f, size, mtime in con.execute(
                "SELECT filename, size, mtime_ns FROM src_files WHERE starts_with(filename, ?)", [prefix]
            ).fetchall()
        }
        changed = sorted(f for f, stat in on_disk.items() if known.get(f) != stat)
        gone = sorted(set(known) - set(on_disk))

        stale = changed + gone
        if stale:
            con.execute("CREATE OR REPLACE TEMP TABLE stale AS SELECT unnest(?) AS filename", [stale])
            for table in (*REC_TABLES, "src_files"):
                con.execute(f"DELETE FROM {table} WHERE filename IN (SELECT filename FROM stale)")
        if changed:
            con.execute(_sql(script, FILES=_sql_list(changed)))
            con.execute(
                "INSERT INTO src_files SELECT unnest(?), unnest(?), unnest(?)",
                [changed, [on_disk[f][0] for f in changed], [on_disk[f][1] for f in changed]],
            )
        files += len(on_disk)
        ingested += len(changed)
        removed += len(gone)
    return BuildStats(files=files, ingested=ingested, removed=removed)


_META_COLUMNS = {"agentType": "VARCHAR", "description": "VARCHAR", "toolUseId": "VARCHAR",
                 "spawnDepth": "INTEGER", "model": "VARCHAR"}


def _meta_source(claude_dir: Path | None) -> str:
    """FROM-clause for the Claude subagent meta files (an empty table if there are none: read_json errors on no match)."""
    cols = ", ".join(f"{k}: '{v}'" for k, v in _META_COLUMNS.items())
    files = sorted(str(p) for p in claude_dir.rglob("subagents/*.meta.json")) if claude_dir else []
    if files:
        return (f"read_json({_sql_list(files)}, union_by_name = true, ignore_errors = true, filename = true, "
                f"columns = {{{cols}}})")
    empty = ", ".join(f"NULL::{v} AS {k}" for k, v in _META_COLUMNS.items())
    return f"(SELECT NULL::VARCHAR AS filename, {empty} WHERE false)"


def build(
    con: duckdb.DuckDBPyConnection,
    claude_dir: Path | None,
    codex_dir: Path | None = None,
    scope: str = "workflow",
    since: str = "1970-01-01",
    rebuild: bool = False,
    prices: dict[str, pricing.Price] | None = None,
) -> BuildStats:
    """Ingest the given log directories (None = skip that provider) and derive everything from the cache."""
    sources = {
        provider: directory.expanduser()
        for provider, directory in (("claude", claude_dir), ("codex", codex_dir))
        if directory is not None and directory.expanduser().is_dir()
    }
    # The JSON reader keeps a buffer of `maximum_object_size` (16 MB in the ingest scripts) per thread: on a busy,
    # many-core machine that is gigabytes for no gain, since the log files are read in a few seconds anyway.
    con.execute(f"SET threads = {min(os.cpu_count() or 4, 8)}")
    stats = ingest(con, sources, rebuild=rebuild)
    con.execute(_sql(
        "02_messages.sql",
        META_SOURCE=_meta_source(sources.get("claude")),
        SINCE=_quote(since),
        PROVIDERS=", ".join(f"'{p}'" for p in sources) or "''",
    ))
    con.execute(_sql("03_markers.sql"))
    assign_phases(con, scope)
    rows = ", ".join(f"('{_quote(m)}', {i}, {o}, {c})" for m, i, o, c in pricing.price_rows(prices))
    con.execute(_sql(
        "04_facts.sql",
        SINCE=_quote(since),
        PRICE_ROWS=rows,
        FALLBACK_MODEL=pricing.FALLBACK_MODEL,
        CW5=str(pricing.CACHE_WRITE_5M_FACTOR),
        CW1H=str(pricing.CACHE_WRITE_1H_FACTOR),
    ))
    con.execute(_sql("05_views.sql"))
    return stats
