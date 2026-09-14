from bmsdna.devtools.cli_tools import AgentSession, detect_agent_session, ensure_agent_session_note, is_claude_code


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
