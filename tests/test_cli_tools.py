from bmsdna.devtools.cli_tools import is_claude_code


def test_is_claude_code_true_when_env_var_set(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    assert is_claude_code() is True


def test_is_claude_code_false_when_env_var_absent(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert is_claude_code() is False


def test_is_claude_code_false_for_unexpected_value(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "0")
    assert is_claude_code() is False
