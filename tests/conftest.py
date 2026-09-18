import pytest

# Every env var `detect_agent_session` (bmsdna.devtools.cli_tools) looks at. Cleared before each
# test so the suite is deterministic regardless of the ambient environment it happens to run in
# -- notably, running these tests from inside Claude Code (or another agent following the same
# AI_AGENT convention) would otherwise make every test that asserts an exact PR/issue/comment body
# fail, since bdt would genuinely detect that agent and append a session note.
_AGENT_ENV_VARS = (
    "CLAUDECODE",
    "AI_AGENT",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "GOOSE_SESSION",
    "OPENCODE",
    "OPENCODE_SESSION_ID",
)


@pytest.fixture(autouse=True)
def _no_agent_session(monkeypatch):
    for var in _AGENT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
