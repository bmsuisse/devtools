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
