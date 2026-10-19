"""`bdt usage`: synthetic Claude Code transcripts and codex rollouts through the whole pipeline."""

from __future__ import annotations

import base64
import json
import re
import struct
from pathlib import Path

import pytest
from typer.testing import CliRunner

duckdb = pytest.importorskip("duckdb")

from bmsdna.devtools.cli import app  # noqa: E402
from bmsdna.devtools.usage import pipeline, pricing  # noqa: E402

runner = CliRunner()

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(result) -> str:
    """The command's output without colour codes: CI renders typer's error box in colour, which splits option names."""
    return ANSI.sub("", result.output)

CLAUDE_SESSION = "11111111-1111-1111-1111-111111111111"
OTHER_SESSION = "22222222-2222-2222-2222-222222222222"
WORKTREE = "/repo/.worktrees/feature"


# -- Claude Code ------------------------------------------------------------------------------------------------


def _usage(inp=10, out=5, read=1000, write=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
            "cache_creation": {"ephemeral_1h_input_tokens": write, "ephemeral_5m_input_tokens": 0}}


def _assistant(mid, sid, minute, tool=None, out=5, sidechain=False, model="claude-sonnet-5-5", cwd=WORKTREE):
    content = [{"type": "text", "text": "ok"}]
    if tool:
        name, tid, inp = tool
        content = [{"type": "tool_use", "id": tid, "name": name, "input": inp}]
    return {"type": "assistant", "timestamp": f"2026-10-01T10:{minute:02d}:00.000Z", "sessionId": sid,
            "isSidechain": sidechain, "cwd": cwd,
            "message": {"id": mid, "model": model, "usage": _usage(out=out), "content": content}}


def _write(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _png_b64(w, h) -> str:
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    return base64.b64encode(header).decode()


def _claude_logs(root: Path, extra_main=()) -> Path:
    proj = root / "claude"
    s1, s2 = CLAUDE_SESSION, OTHER_SESSION
    _write(proj / f"{s1}.jsonl", [
        _assistant("m0", s1, 0),  # before the workflow starts
        _assistant("m1", s1, 1, ("Skill", "t1", {"skill": "dev-workflow:dev-workflow"})),
        _assistant("m2", s1, 2, ("Bash", "t2", {"command": "bdt pr create --draft"})),
        _assistant("m3", s1, 3, ("Edit", "t3", {"file_path": "/repo/src/a.py"}), out=3),
        _assistant("m3", s1, 3, ("Edit", "t3", {"file_path": "/repo/src/a.py"}), out=40),  # streamed duplicate
        _assistant("m4", s1, 4, ("Bash", "t4", {"command": "uv run pytest tests/"})),
        _assistant("m5", s1, 5),  # back to implement after the one-off test turn
        {"type": "user", "timestamp": "2026-10-01T10:06:00.000Z", "sessionId": s1, "isSidechain": False,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9", "content": [
             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png_b64(1000, 750)}}]}]}},
        _assistant("m6", s1, 7, ("Skill", "t6", {"skill": "dev-workflow:bms-code-review", "args": "--full"})),
        _assistant("m7", s1, 8, ("Agent", "toolu_A", {"subagent_type": "dev-workflow:bms-review-core", "description": "x", "prompt": "review"})),
        _assistant("m8", s1, 20, ("Bash", "t8", {"command": f'codex exec -C {WORKTREE} -s read-only "Use the bms-code-review skill"'})),
        *extra_main,
    ])
    sub = proj / s1 / "subagents"
    _write(sub / "agent-AAA.jsonl", [
        {**_assistant("s1", s1, 9, sidechain=True), "agentId": "AAA"},
        {**_assistant("s2", s1, 10, ("Bash", "t20", {"command": "uv run pytest"}), sidechain=True), "agentId": "AAA"},
    ])
    (sub / "agent-AAA.meta.json").write_text(json.dumps(
        {"agentType": "dev-workflow:bms-review-core", "description": "review", "toolUseId": "toolu_A", "spawnDepth": 1}))
    _write(proj / f"{s2}.jsonl", [_assistant("o1", s2, 0), _assistant("o2", s2, 1, ("Bash", "u1", {"command": "pytest"}))])
    return proj


# -- codex ------------------------------------------------------------------------------------------------------


def _rollout(path: Path, thread_id: str, *, minute: int, cwd: str, parent: str | None = None, session: str | None = None,
             model="gpt-6-sol", first_message="Do a thorough code review", agent_path=None, responses=()):
    """responses: (response_id, [tool item payloads], (input, cached, output)) -> tool items logged before the usage record."""
    ts = lambda sec: f"2026-10-01T10:{minute:02d}:{sec:02d}.000Z"  # noqa: E731
    meta = {"id": thread_id, "session_id": session or thread_id, "cwd": cwd,
            "thread_source": "subagent" if parent else "user"}
    if parent:
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        meta["agent_path"] = agent_path
    records = [
        {"type": "session_meta", "timestamp": ts(0), "ordinal": 0, "payload": meta},
        {"type": "turn_context", "timestamp": ts(1), "ordinal": 1, "payload": {"model": model}},
        {"type": "event_msg", "timestamp": ts(1), "ordinal": 2, "payload": {
            "type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": first_message}]}}},
    ]
    ordinal = 3
    for i, (rid, items, (inp, cached, out)) in enumerate(responses):
        for item in items:
            records.append({"type": "response_item", "timestamp": ts(2 + i), "ordinal": ordinal, "payload": item})
            ordinal += 1
        records.append({"type": "token_usage_record", "timestamp": ts(2 + i), "ordinal": ordinal, "payload": {
            "response_id": rid, "usage": {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
                                          "output_tokens": out, "total_tokens": inp + out}}})
        ordinal += 1
    _write(path, records)


def _exec(script: str) -> dict:
    return {"type": "custom_tool_call", "call_id": f"call_{abs(hash(script)) % 10**6}", "name": "exec", "input": script}


def _spawn(task: str) -> dict:
    return {"type": "function_call", "call_id": f"call_{task}", "name": "spawn_agent", "namespace": "collaboration",
            "arguments": json.dumps({"task_name": task, "message": "gAAAA-encrypted"})}


def _codex_logs(root: Path) -> Path:
    cdx = root / "codex" / "2026" / "10" / "01"
    # a review run launched by the Claude call m8 (same worktree, 10 s later) which spawns one subagent
    _rollout(cdx / "rollout-root.jsonl", "R", minute=20, cwd=WORKTREE, session="R", responses=[
        ("resp_1", [_exec("cat ~/.codex/skills/bms-code-review/SKILL.md")], (1000, 400, 50)),
        ("resp_2", [_spawn("correctness_security")], (1500, 1000, 30)),
    ])
    _rollout(cdx / "rollout-child.jsonl", "C", minute=21, cwd=WORKTREE, session="R", parent="R", agent_path="/root/correctness_security",
             responses=[("resp_3", [_exec("git diff origin/main...HEAD")], (800, 500, 20))])
    # a standalone codex session in another directory, nothing to do with a Claude session
    _rollout(cdx / "rollout-solo.jsonl", "X", minute=40, cwd="/elsewhere", model="gpt-6-luna",
             responses=[("resp_4", [_exec("bdt pr create --draft")], (100, 0, 10))])
    return root / "codex"


@pytest.fixture
def con(tmp_path):
    c = duckdb.connect()
    pipeline.build(c, _claude_logs(tmp_path), _codex_logs(tmp_path))
    return c


def phases(con) -> dict[str, str]:
    return dict(con.execute("SELECT mid, phase FROM msg_phase").fetchall())


# -- Claude Code behaviour ----------------------------------------------------------------------------------------


def test_phase_assignment(con) -> None:
    p = phases(con)
    assert p["m0"] == "outside"
    assert p["m1"] == "setup"
    assert p["m2"] == "plan"
    assert p["m3"] == "implement"
    assert p["m4"] == "test"  # test runs only count for their own turn
    assert p["m5"] == "implement"
    assert p["m6"] == "review_full"


def test_other_sessions_stay_out_of_scope(con) -> None:
    assert phases(con)["o1"] == phases(con)["o2"] == "outside"


def test_subagent_inherits_review_phase_and_ignores_commands(con) -> None:
    p = phases(con)
    assert p["s1"] == p["s2"] == "review_full"  # pytest inside a reviewer is still the review


def test_streamed_duplicates_collapse_to_final_usage(con) -> None:
    assert con.execute("SELECT count(*), max(out_tok) FROM msgs WHERE mid = 'm3'").fetchone() == (1, 40)


def test_cost_uses_per_model_prices(con) -> None:
    # m0: 10 input @ $2/M + 1000 cache reads @ $0.20/M + 5 output @ $10/M
    (cost,) = con.execute("SELECT cost FROM facts WHERE mid = 'm0'").fetchone()
    assert cost == pytest.approx((10 * 2 + 1000 * 0.2 + 5 * 10) / 1e6)


def test_image_tokens_from_png_header(con) -> None:
    assert con.execute("SELECT est_tokens FROM images").fetchone() == (1000,)
    carried = dict(con.execute("SELECT mid, img_ctx_tok FROM ctx_img").fetchall())
    assert carried["m5"] == 0 and carried["m6"] == 1000


def test_shares_sum_to_100(con) -> None:
    (total,) = con.execute("SELECT sum(pct_cost) FROM v_steps").fetchone()
    assert total == pytest.approx(100)


# -- codex ------------------------------------------------------------------------------------------------------


def test_codex_usage_counts_cached_tokens_once(con) -> None:
    # input 1000 of which 400 cached: 600 fresh input + 400 cache reads (cached is part of input, not on top of it)
    row = con.execute("SELECT in_tok, cache_read_tok, cache_write_tok, out_tok, model, provider FROM msgs WHERE mid = 'resp_1'").fetchone()
    assert row == (600, 400, 0, 50, "gpt-6-sol", "codex")


def test_codex_tool_calls_belong_to_the_response_that_follows_them(con) -> None:
    blocks = {mid: json.loads(b) for mid, b in con.execute("SELECT mid, tool_blocks FROM rec_assistant WHERE provider = 'codex'").fetchall()}
    assert [b["name"] for b in blocks["resp_1"]] == ["Bash"] and "bms-code-review/SKILL.md" in blocks["resp_1"][0]["input"]["command"]
    assert [b["name"] for b in blocks["resp_2"]] == ["Agent"] and blocks["resp_2"][0]["input"]["description"] == "correctness_security"


def test_codex_run_launched_by_claude_is_linked_to_that_call(con) -> None:
    rows = dict(con.execute("SELECT thread_id, parent_mid FROM threads WHERE provider = 'codex'").fetchall())
    assert rows["R"] == "m8"  # the Claude call that ran `codex exec -C <worktree>`
    child_parent = rows["C"]
    assert child_parent == "resp_2"  # the parent's latest response before the child started
    assert "X" not in rows  # unrelated codex session: a main thread


def test_codex_review_counts_as_full_review_of_the_claude_session(con) -> None:
    p = phases(con)
    assert p["resp_1"] == p["resp_2"] == p["resp_3"] == "review_full"
    sessions = dict(con.execute("SELECT mid, session_id FROM msgs WHERE provider = 'codex'").fetchall())
    assert sessions["resp_1"] == sessions["resp_3"] == CLAUDE_SESSION  # re-homed, subagent included
    assert sessions["resp_4"] == "X"


def test_standalone_codex_session_is_out_of_scope_unless_scope_all(tmp_path, con) -> None:
    assert phases(con)["resp_4"] == "outside"
    c = duckdb.connect()
    pipeline.build(c, tmp_path / "claude", tmp_path / "codex", scope="all")
    assert phases(c)["resp_4"] == "plan"  # `bdt pr create` in a thread that follows the workflow from its first turn


def test_codex_models_have_no_price_unless_given(con, tmp_path) -> None:
    assert con.execute("SELECT count(*) FROM facts WHERE provider = 'codex' AND cost IS NULL AND NOT priced").fetchone() == (4,)
    assert con.execute("SELECT unpriced_msgs, unpriced_models FROM v_summary").fetchone() == (3, "gpt-6-sol")  # in scope: resp_1..3

    c = duckdb.connect()
    pipeline.build(c, tmp_path / "claude", tmp_path / "codex", prices={"gpt-6-sol": pricing.Price(2.0, 10.0, 0.5)})
    (cost,) = c.execute("SELECT cost FROM facts WHERE mid = 'resp_1'").fetchone()
    assert cost == pytest.approx((600 * 2.0 + 400 * 0.5 + 50 * 10.0) / 1e6)


def test_unknown_claude_model_is_priced_like_the_fallback_but_other_models_are_not(tmp_path) -> None:
    proj = _claude_logs(tmp_path, extra_main=[_assistant("mx", CLAUDE_SESSION, 30, model="claude-future-9")])
    c = duckdb.connect()
    pipeline.build(c, proj)
    assert c.execute("SELECT priced FROM facts WHERE mid = 'mx'").fetchone() == (True,)


def test_providers_can_be_left_out(tmp_path) -> None:
    _claude_logs(tmp_path)
    _codex_logs(tmp_path)
    c = duckdb.connect()
    pipeline.build(c, tmp_path / "claude", None)
    assert c.execute("SELECT count(*) FROM facts WHERE provider = 'codex'").fetchone() == (0,)
    assert phases(c)["m6"] == "review_full"


# -- parse cache -------------------------------------------------------------------------------------------------


def test_second_build_reads_nothing_and_only_changed_files_are_reparsed(tmp_path) -> None:
    proj = tmp_path / "proj"
    sid = "33333333-3333-3333-3333-333333333333"
    _write(proj / f"{sid}.jsonl", [_assistant("a1", sid, 0, ("Skill", "t1", {"skill": "dev-workflow"}))])
    _write(proj / "other" / "44444444-4444-4444-4444-444444444444.jsonl", [_assistant("b1", "44444444-4444-4444-4444-444444444444", 0)])
    c = duckdb.connect()

    first = pipeline.build(c, proj)
    assert (first.files, first.ingested) == (2, 2)
    assert pipeline.build(c, proj).ingested == 0

    # an active session appends a message: only that file is parsed again, nothing is duplicated
    path = proj / f"{sid}.jsonl"
    path.write_text(path.read_text() + json.dumps(_assistant("a2", sid, 1, ("Bash", "t2", {"command": "bdt pr create"}))) + "\n")
    third = pipeline.build(c, proj)
    assert third.ingested == 1
    assert c.execute("SELECT count(*) FROM msgs").fetchone() == (3,)
    assert dict(c.execute("SELECT mid, phase FROM msg_phase").fetchall())["a2"] == "plan"

    path.unlink()
    assert pipeline.build(c, proj).removed == 1
    assert c.execute("SELECT count(*) FROM msgs").fetchone() == (1,)


def test_rebuild_flag_rereads_everything(tmp_path) -> None:
    proj = tmp_path / "proj"
    _write(proj / "s.jsonl", [_assistant("a1", "s", 0)])
    c = duckdb.connect()
    pipeline.build(c, proj)
    assert pipeline.build(c, proj, rebuild=True).ingested == 1


def test_skipping_a_provider_does_not_drop_its_cache(tmp_path) -> None:
    c = duckdb.connect()
    claude, codex = _claude_logs(tmp_path), _codex_logs(tmp_path)
    pipeline.build(c, claude, codex)
    again = pipeline.build(c, claude, None)
    assert again.removed == 0 and again.ingested == 0
    assert pipeline.build(c, claude, codex).ingested == 0


# -- CLI ------------------------------------------------------------------------------------------------------------


def test_cli_analyze_writes_database_and_report(tmp_path) -> None:
    _claude_logs(tmp_path)
    _codex_logs(tmp_path)
    out = tmp_path / "out"

    result = runner.invoke(app, ["usage", "analyze", "--logs", str(tmp_path / "claude"), "--codex-logs", str(tmp_path / "codex"),
                                 "--db", str(out / "p.duckdb"), "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert (out / "p.duckdb").exists() and (out / "facts.parquet").exists()
    html = (out / "report.html").read_text()
    assert "of tokens in codex" in html and "have no price" in html and "gpt-6-sol" in html
    assert "Review (full, pre-publish)" in result.output
    assert "3 turns of gpt-6-sol have no price" in result.output


def test_cli_analyze_with_price_has_no_unpriced_note(tmp_path) -> None:
    _claude_logs(tmp_path)
    _codex_logs(tmp_path)

    result = runner.invoke(app, ["usage", "analyze", "--logs", str(tmp_path / "claude"), "--codex-logs", str(tmp_path / "codex"),
                                 "--db", str(tmp_path / "p.duckdb"), "--no-report", "--price", "gpt-6-sol=2,10,0.5"])

    assert result.exit_code == 0, result.output
    assert "have no price" not in result.output


def test_cli_sql_queries_the_last_analysis(tmp_path) -> None:
    _claude_logs(tmp_path)
    db = str(tmp_path / "p.duckdb")
    runner.invoke(app, ["usage", "analyze", "--logs", str(tmp_path / "claude"), "--no-codex", "--db", db, "--no-report"])

    result = runner.invoke(app, ["usage", "sql", "--db", db, "select count(*) as n from facts"])

    assert result.exit_code == 0, result.output
    assert "n" in result.output


def test_cli_sql_without_a_database_says_what_to_do(tmp_path) -> None:
    result = runner.invoke(app, ["usage", "sql", "--db", str(tmp_path / "nope.duckdb"), "select 1"])

    assert result.exit_code == 1
    assert "bdt usage analyze" in result.output


@pytest.mark.parametrize("bad", ["gpt", "gpt=1,2", "gpt=a,b,c", "gpt=-1,2,3", "=1,2,3"])
def test_cli_rejects_a_malformed_price(tmp_path, bad) -> None:
    result = runner.invoke(app, ["usage", "analyze", "--logs", str(tmp_path), "--db", str(tmp_path / "p.duckdb"), "--price", bad])

    assert result.exit_code == 2, result.output


def test_cli_rejects_an_unknown_scope(tmp_path) -> None:
    result = runner.invoke(app, ["usage", "analyze", "--logs", str(tmp_path), "--scope", "everything"])

    assert result.exit_code == 2


def test_cli_with_nothing_in_scope_says_so(tmp_path) -> None:
    _write(tmp_path / "claude" / "s.jsonl", [_assistant("a1", "s", 0)])

    result = runner.invoke(app, ["usage", "analyze", "--logs", str(tmp_path / "claude"), "--no-codex", "--db", str(tmp_path / "p.duckdb")])

    assert result.exit_code == 1
    assert "--scope all" in result.output


def test_cli_prints_an_install_hint_without_duckdb(tmp_path, monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_duckdb(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("no duckdb")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_duckdb)

    result = runner.invoke(app, ["usage", "analyze", "--logs", str(tmp_path), "--db", str(tmp_path / "p.duckdb")])

    assert result.exit_code == 1
    assert "bmsdna-devtools[usage]" in result.output


def test_report_renders_when_nothing_in_scope_has_a_price(tmp_path) -> None:
    from bmsdna.devtools.usage import report

    _codex_logs(tmp_path)
    c = duckdb.connect()
    pipeline.build(c, None, tmp_path / "codex", scope="all")

    html = report.write_report(c, tmp_path / "out").read_text()

    assert "have no price" in html


# -- findings from the codex review of this PR ----------------------------------------------------------------------


def test_since_drops_turns_before_the_cutoff(tmp_path) -> None:
    proj = tmp_path / "proj"
    sid = "55555555-5555-5555-5555-555555555555"
    _write(proj / f"{sid}.jsonl", [_assistant("a", sid, 0, ("Skill", "t1", {"skill": "dev-workflow:dev-workflow"})), _assistant("b", sid, 1)])
    c = duckdb.connect()

    pipeline.build(c, proj, since="2026-10-01")
    assert c.execute("SELECT count(*) FROM facts").fetchone() == (2,)

    pipeline.build(c, proj, since="2026-10-02")
    assert c.execute("SELECT count(*) FROM facts").fetchone() == (0,)


def test_since_keeps_a_later_turn_of_an_earlier_workflow(tmp_path) -> None:
    proj = tmp_path / "proj"
    sid = "66666666-6666-6666-6666-666666666666"
    early = {**_assistant("early", sid, 0, ("Skill", "t1", {"skill": "dev-workflow:dev-workflow"})), "timestamp": "2026-09-30T10:00:00.000Z"}
    _write(proj / f"{sid}.jsonl", [early, _assistant("late", sid, 5, ("Edit", "t3", {"file_path": "/repo/src/a.py"}))])
    c = duckdb.connect()

    pipeline.build(c, proj, since="2026-10-01")

    assert dict(c.execute("SELECT mid, phase FROM facts").fetchall()) == {"late": "implement"}


def test_report_output_directory_may_contain_an_apostrophe(tmp_path) -> None:
    from bmsdna.devtools.usage import report

    _claude_logs(tmp_path)
    c = duckdb.connect()
    pipeline.build(c, tmp_path / "claude", None)
    out = tmp_path / "Alice's report"

    report.write_report(c, out)

    assert (out / "report.html").exists() and (out / "facts.parquet").exists() and (out / "csv" / "v_steps.csv").exists()


def test_text_from_the_logs_cannot_break_out_of_the_report(tmp_path) -> None:
    """Agent descriptions / session titles come from the logs: they must stay text, never markup or script."""
    from bmsdna.devtools.usage import report

    evil = "</script><script>window.PWNED=1</script>"
    proj = _claude_logs(tmp_path)
    meta = next(proj.rglob("agent-AAA.meta.json"))
    meta.write_text(meta.read_text().replace('"description": "review"', f'"description": {json.dumps(evil)}'))
    main = proj / f"{CLAUDE_SESSION}.jsonl"
    main.write_text(main.read_text() + json.dumps({"type": "ai-title", "sessionId": CLAUDE_SESSION, "aiTitle": evil}) + "\n")
    c = duckdb.connect()
    pipeline.build(c, proj, None)

    html = report.write_report(c, tmp_path / "out").read_text()

    assert "PWNED" in html  # it is in the report (as an escaped string), so this test is not vacuous
    assert evil not in html and "<script>window.PWNED" not in html


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-01", "2026-10-01'; DROP TABLE x; --", ""])
def test_cli_rejects_a_since_that_is_not_a_date(tmp_path, bad) -> None:
    result = runner.invoke(app, ["usage", "analyze", "--logs", str(tmp_path), "--since", bad, "--db", str(tmp_path / "p.duckdb")])

    assert result.exit_code == 2
    assert "--since" in plain(result)
