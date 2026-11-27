import sys

import pytest

from bmsdna.devtools import ci_warnings

ESC = "\x1b"


def script(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_announce_per_ci_system(capsys: pytest.CaptureFixture[str]) -> None:
    ci_warnings.announce(["a%b\nc"], {"TF_BUILD": "True"})
    ci_warnings.announce(["a%b\nc"], {"GITHUB_ACTIONS": "true"})
    ci_warnings.announce(["a"], {})
    assert capsys.readouterr().out.splitlines() == [
        "##vso[task.logissue type=warning]a%AZP25b%0Ac",
        "::warning::a%25b%0Ac",
        "warning: a",
    ]


def test_run_passes_the_output_through_and_announces_the_warnings(capsys: pytest.CaptureFixture[str]) -> None:
    code = f"print('building'); print('{ESC}[33m[ODD_IMPORT] {ESC}[0mx is imported twice.')"
    assert ci_warnings.run(script(code), {"TF_BUILD": "True"}) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "building" and out[-1] == "##vso[task.logissue type=warning][ODD_IMPORT] x is imported twice."


def test_run_keeps_the_exit_code_and_still_announces(capsys: pytest.CaptureFixture[str]) -> None:
    code = "import sys; print('(!) slow'); sys.exit(3)"
    assert ci_warnings.run(script(code), {}) == 3
    assert "warning: slow" in capsys.readouterr().out


def test_run_without_warnings_adds_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_warnings.run(script("print('fine')"), {"TF_BUILD": "True"}) == 0
    assert capsys.readouterr().out == "fine\n"


def test_run_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_warnings.run(["definitely-not-a-command-xyz"], {}) == 127
    assert "command not found" in capsys.readouterr().err
