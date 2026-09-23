from bmsdna.devtools.cli_tools import (
    AgentSession,
    PollHeartbeat,
    detect_agent_session,
    ensure_agent_session_note,
    is_claude_code,
)


def test_is_claude_code_true_when_env_var_set(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    assert is_claude_code() is True


def test_is_claude_code_false_when_env_var_absent(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert is_claude_code() is False


def test_is_claude_code_false_for_unexpected_value(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "0")
    assert is_claude_code() is False


def test_detect_agent_session_none_by_default() -> None:
    assert detect_agent_session() is None


def test_detect_agent_session_claude_prefers_bridge_link(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "local-uuid")
    assert detect_agent_session() == AgentSession("Claude", "https://claude.ai/code/session_abc123")


def test_detect_agent_session_claude_falls_back_to_local_session_id(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("CLAUDE_CODE_BRIDGE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "local-uuid")
    assert detect_agent_session() == AgentSession("Claude", "local-uuid")


def test_detect_agent_session_claude_without_any_session_id_is_none(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("CLAUDE_CODE_BRIDGE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert detect_agent_session() is None


def test_detect_agent_session_ai_agent_resolves_known_tool_session_id(monkeypatch) -> None:
    monkeypatch.setenv("AI_AGENT", "goose_1-2-3_agent")
    monkeypatch.setenv("GOOSE_SESSION", "goose-session-id")
    assert detect_agent_session() == AgentSession("Goose", "goose-session-id")


def test_detect_agent_session_ai_agent_unknown_tool_records_raw_tag(monkeypatch) -> None:
    monkeypatch.setenv("AI_AGENT", "some-new-tool_9-9-9_agent")
    assert detect_agent_session() == AgentSession("Some New Tool", "some-new-tool_9-9-9_agent")


def test_detect_agent_session_falls_back_to_goose_env_without_ai_agent(monkeypatch) -> None:
    monkeypatch.setenv("GOOSE_SESSION", "goose-session-id")
    assert detect_agent_session() == AgentSession("Goose", "goose-session-id")


def test_detect_agent_session_falls_back_to_opencode_env_without_ai_agent(monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE", "1")
    monkeypatch.setenv("OPENCODE_SESSION_ID", "opencode-session-id")
    assert detect_agent_session() == AgentSession("OpenCode", "opencode-session-id")


def test_ensure_agent_session_note_no_agent_returns_text_unchanged(monkeypatch) -> None:
    assert ensure_agent_session_note("Some description") == "Some description"
    assert ensure_agent_session_note(None) is None


def test_ensure_agent_session_note_appends_when_missing(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    result = ensure_agent_session_note("Fixes the bug")
    assert result == "Fixes the bug\n\nClaude Session: https://claude.ai/code/session_abc123"


def test_ensure_agent_session_note_handles_none_text(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    assert ensure_agent_session_note(None) == "Claude Session: https://claude.ai/code/session_abc123"


def test_ensure_agent_session_note_skips_if_already_in_text(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    text = "Already noted.\n\nClaude Session: https://claude.ai/code/session_abc123"
    assert ensure_agent_session_note(text) == text


def test_ensure_agent_session_note_skips_if_already_in_title(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_abc123")
    description = "Some description"
    title = "Fix bug (Claude Session: https://claude.ai/code/session_abc123)"
    assert ensure_agent_session_note(description, also_check=title) == description


def test_poll_heartbeat_prints_first_call(capsys) -> None:
    heartbeat = PollHeartbeat()
    heartbeat.show("\rstatus: pending")
    assert capsys.readouterr().out == "\rstatus: pending"


def test_poll_heartbeat_suppresses_unchanged_status_before_interval(monkeypatch, capsys) -> None:
    clock = [0.0]
    monkeypatch.setattr("bmsdna.devtools.cli_tools.time.monotonic", lambda: clock[0])
    heartbeat = PollHeartbeat(heartbeat_secs=600)

    heartbeat.show("\rstatus: pending")
    capsys.readouterr()

    clock[0] = 300  # well under the 600s heartbeat interval
    heartbeat.show("\rstatus: pending")
    assert capsys.readouterr().out == ""


def test_poll_heartbeat_reprints_unchanged_status_after_interval(monkeypatch, capsys) -> None:
    clock = [0.0]
    monkeypatch.setattr("bmsdna.devtools.cli_tools.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("bmsdna.devtools.cli_tools.time.strftime", lambda fmt: "12:00:00")
    heartbeat = PollHeartbeat(heartbeat_secs=600)

    heartbeat.show("\rstatus: pending")
    capsys.readouterr()

    clock[0] = 600  # exactly at the heartbeat interval
    heartbeat.show("\rstatus: pending")
    out = capsys.readouterr().out
    assert out == "\n[12:00:00] still waiting: status: pending\n"


def test_poll_heartbeat_always_prints_a_changed_status(monkeypatch, capsys) -> None:
    clock = [0.0]
    monkeypatch.setattr("bmsdna.devtools.cli_tools.time.monotonic", lambda: clock[0])
    heartbeat = PollHeartbeat(heartbeat_secs=600)

    heartbeat.show("\rstatus: pending")
    capsys.readouterr()

    clock[0] = 30  # far under the heartbeat interval, but the status itself changed
    heartbeat.show("\rstatus: passed")
    assert capsys.readouterr().out == "\rstatus: passed"


def test_poll_heartbeat_resets_interval_after_a_real_change(monkeypatch, capsys) -> None:
    clock = [0.0]
    monkeypatch.setattr("bmsdna.devtools.cli_tools.time.monotonic", lambda: clock[0])
    heartbeat = PollHeartbeat(heartbeat_secs=600)

    heartbeat.show("\rstatus: pending")
    clock[0] = 300
    heartbeat.show("\rstatus: passed")
    capsys.readouterr()

    clock[0] = 700  # 400s after the last (changed) print -- still under a fresh 600s window
    heartbeat.show("\rstatus: passed")
    assert capsys.readouterr().out == ""
