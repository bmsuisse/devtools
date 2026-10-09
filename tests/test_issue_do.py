from bmsdna.devtools import issue_do


def test_claude_command_names_session_and_is_headless() -> None:
    cmd = issue_do.build_command("claude", 60, "add command", "body text", ["--model", "opus"])
    assert cmd[:2] == ["claude", "-p"]
    assert "body text" in cmd[2]
    assert cmd[3:5] == ["--name", "60: add command"]
    assert cmd[-2:] == ["--model", "opus"]


def test_other_agent_gets_prompt_last() -> None:
    cmd = issue_do.build_command("codex", 7, "t", "", ["exec"])
    assert cmd[:2] == ["codex", "exec"]
    assert "(no description)" in cmd[2]


def test_dry_run_prints(capsys) -> None:
    issue_do.run(1, "x", "y", agent="claude", extra=[], dry_run=True)
    assert '"1: x"' in capsys.readouterr().out


def test_claude_gets_the_given_session_id() -> None:
    cmd = issue_do.build_command("claude", 60, "t", "b", ["--model", "opus"], session_id="abc")
    assert cmd[cmd.index("--session-id") + 1] == "abc"
    assert cmd[-2:] == ["--model", "opus"]


def test_take_message_names_the_session() -> None:
    assert issue_do.take_message("me", "claude", "abc") == "Taken by me\n\nClaude Session: abc (resume with `claude --resume abc`)"
    assert issue_do.take_message("me", "codex", None) == "Taken by me (via codex)"
