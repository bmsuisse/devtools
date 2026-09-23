import base64
import subprocess

import pytest

from bmsdna.devtools.ado_auth import auth_header, get_az_devops_token
from bmsdna.devtools.cli_tools import CLI_TIMEOUT_SECS


def test_auth_header_uses_pat_without_calling_az(monkeypatch) -> None:
    def fail_run(*args, **kwargs):
        raise AssertionError("must not shell out to az when a PAT is given")

    monkeypatch.setattr("bmsdna.devtools.ado_auth.subprocess.run", fail_run)

    header = auth_header("my-pat")

    token = base64.b64encode(b":my-pat").decode()
    assert header == {"Authorization": f"Basic {token}"}


def test_get_az_devops_token_returns_stripped_stdout(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.ado_auth.require_az", lambda: "az")

    def fake_run(cmd, **kwargs):
        assert kwargs.get("timeout") == CLI_TIMEOUT_SECS
        return type("Result", (), {"returncode": 0, "stdout": "tokenvalue\n", "stderr": ""})()

    monkeypatch.setattr("bmsdna.devtools.ado_auth.subprocess.run", fake_run)

    assert get_az_devops_token() == "tokenvalue"


def test_get_az_devops_token_exits_on_failed_login(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.ado_auth.require_az", lambda: "az")

    def fake_run(cmd, **kwargs):
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "not logged in"})()

    monkeypatch.setattr("bmsdna.devtools.ado_auth.subprocess.run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        get_az_devops_token()

    assert "az login required" in str(exc_info.value)


def test_get_az_devops_token_times_out_with_clear_message_not_a_hang(monkeypatch) -> None:
    """Regression: `az` blocking on an interactive re-auth prompt (an expired cached login)
    must exit with a clear message within CLI_TIMEOUT_SECS, not hang forever.
    """
    monkeypatch.setattr("bmsdna.devtools.ado_auth.require_az", lambda: "az")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr("bmsdna.devtools.ado_auth.subprocess.run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        get_az_devops_token()

    assert "timed out" in str(exc_info.value)
