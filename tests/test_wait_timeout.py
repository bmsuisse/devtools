"""`pr status --wait --timeout N`: give up after N seconds with the current state and exit code 4, so a caller
whose own time limit is shorter than CI (an agent's shell tool, say) gets a result instead of being cut off."""

from __future__ import annotations

import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.cli_tools import (
    ENCLOSING_TIMEOUT_MARGIN_SECS,
    EXIT_WAIT_TIMEOUT,
    WaitDeadline,
    enclosing_timeout_remaining,
    limit_by_enclosing_timeout,
    parse_timeout_command,
    status_rerun_cmd,
)
from bmsdna.devtools.gh_pr import run as gh_run
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote
from bmsdna.devtools.issue_watch import EXIT_TIMEOUT
from bmsdna.devtools.pr_build import run as ado_run

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_enclosing_timeout(monkeypatch) -> None:
    """Whatever wraps the test run (an agent's `timeout 300 pytest`) must not clamp the waits under test; the tests
    of the detection itself set their own."""
    monkeypatch.setattr("bmsdna.devtools.cli_tools.enclosing_timeout_remaining", lambda: None)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """A fake clock: `time.sleep` only advances `time.monotonic`, and records what it was asked for."""
    now = [1000.0]
    slept: list[float] = []

    def fake_sleep(secs: float) -> None:
        slept.append(secs)
        now[0] += secs
        if len(slept) > 500:
            pytest.fail("polled forever: the --timeout deadline never fired")

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", fake_sleep)
    return slept


def test_exit_code_matches_the_other_wait_commands() -> None:
    assert EXIT_WAIT_TIMEOUT == EXIT_TIMEOUT == 4


def test_deadline_without_timeout_never_expires_and_sleeps_in_full(sleeps) -> None:
    deadline = WaitDeadline(0)
    deadline.sleep(30)
    deadline.sleep(30)

    assert sleeps == [30, 30]
    assert not deadline.expired()


def test_deadline_cuts_the_last_sleep_short(sleeps) -> None:
    deadline = WaitDeadline(70)
    while not deadline.expired():
        deadline.sleep(30)

    assert sleeps == [30, 30, 10]


# -- GitHub -------------------------------------------------------------------


def _gh_pr(checks: list[dict]) -> dict:
    return {
        "number": 42,
        "title": "feat: widgets",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "isDraft": False,
        "statusCheckRollup": checks,
    }


PENDING = {"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS", "workflowName": "CI"}
PASSED = {
    "__typename": "CheckRun",
    "name": "lint",
    "status": "COMPLETED",
    "conclusion": "SUCCESS",
    "workflowName": "CI",
}


def test_gh_wait_times_out_with_exit_4_and_reports_every_check(monkeypatch, sleeps, capsys) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: _gh_pr([PASSED, PENDING]))

    with pytest.raises(SystemExit) as exc:
        gh_run(MagicMock(), wait=True, timeout=70)

    out = capsys.readouterr().out
    assert exc.value.code == EXIT_WAIT_TIMEOUT
    assert sleeps == [30, 30, 10]
    assert "[✓ PASS] CI / lint" in out and "[… PENDING] CI / build" in out
    assert out.rstrip().endswith(
        "Still running after 70 s. Re-run `bdt pr status --pr-id 42 --wait --timeout 70` to keep waiting."
    )


def test_gh_wait_without_timeout_keeps_polling(monkeypatch, sleeps) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: _gh_pr([PENDING]))

    class EnoughError(Exception):
        pass

    def sleep_a_few_times(secs: float) -> None:
        sleeps.append(secs)
        if len(sleeps) > 200:
            raise EnoughError

    monkeypatch.setattr(time, "sleep", sleep_a_few_times)

    with pytest.raises(EnoughError):
        gh_run(MagicMock(), wait=True)

    assert len(sleeps) > 200


def test_gh_wait_returns_normally_when_checks_finish_before_the_deadline(monkeypatch, sleeps, capsys) -> None:
    polls = iter([_gh_pr([PENDING]), _gh_pr([{**PENDING, "status": "COMPLETED", "conclusion": "SUCCESS"}])])
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: next(polls))
    monkeypatch.setattr("bmsdna.devtools.gh_pr.deploy_run_hint", lambda gh, base: None)

    gh_run(MagicMock(), wait=True, timeout=300)

    assert sleeps == [30]
    assert "Still running" not in capsys.readouterr().out


# -- Azure DevOps ---------------------------------------------------------------


def _ado_env(monkeypatch, builds: list[dict]) -> None:
    monkeypatch.setattr("bmsdna.devtools.pr_build.requests.Session", MagicMock)
    monkeypatch.setattr("bmsdna.devtools.ado_auth.auth_header", lambda pat: {})
    monkeypatch.setattr(
        "bmsdna.devtools.pr_build.get_pr",
        lambda *a, **k: {"pullRequestId": 42, "title": "feat: widgets", "status": "active", "isDraft": False},
    )
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_builds_for_pr", lambda *a, **k: builds)
    monkeypatch.setattr("bmsdna.devtools.pr_build.find_pending_approvals", lambda *a, **k: [])
    monkeypatch.setattr(
        "bmsdna.devtools.pr_build.print_build", lambda session, remote, b: print(f"  build #{b['id']} {b['status']}")
    )


def test_ado_wait_times_out_with_exit_4_and_reports_the_running_builds(monkeypatch, sleeps, capsys) -> None:
    _ado_env(monkeypatch, [{"id": 200, "status": "inProgress", "result": None, "definition": {"id": 2, "name": "ci"}}])

    with pytest.raises(SystemExit) as exc:
        ado_run(AdoRemote("myorg", "MyProj", "myrepo"), None, "main", wait=True, source_branch="feature-x", timeout=45)

    out = capsys.readouterr().out
    assert exc.value.code == EXIT_WAIT_TIMEOUT
    assert sleeps == [30, 15]
    assert "ci #200 inProgress" in out and "build #200 inProgress" in out
    assert out.rstrip().endswith(
        "Still running after 45 s. Re-run `bdt pr status --pr-id 42 --wait --timeout 45` to keep waiting."
    )


def test_ado_wait_times_out_while_no_build_has_been_queued_yet(monkeypatch, sleeps, capsys) -> None:
    _ado_env(monkeypatch, [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.lacks_build_policy", lambda *a: False)

    with pytest.raises(SystemExit) as exc:
        ado_run(AdoRemote("myorg", "MyProj", "myrepo"), None, "main", wait=True, source_branch="feature-x", timeout=60)

    assert exc.value.code == EXIT_WAIT_TIMEOUT
    assert "No builds found." in capsys.readouterr().out


# -- CLI ----------------------------------------------------------------------


def test_cli_passes_timeout_on(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GitHubRemote("owner", "repo"))
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.resolve_pr_id", lambda gh, pr_id, target: pr_id)
    seen: dict = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.gh_pr.run", lambda gh, wait, pr_id=None, timeout=0: seen.update(wait=wait, timeout=timeout)
    )

    result = runner.invoke(app, ["pr", "status", "--wait", "--timeout", "540"])

    assert result.exit_code == 0, result.output
    assert seen == {"wait": True, "timeout": 540}


def _plain(output: str) -> str:
    """Rich colors and boxes the error panel on CI terminals; reduce it to the bare words."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", output)).split())


def test_cli_timeout_needs_wait(monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.cli.current_remote", lambda: pytest.fail("must be rejected before anything runs")
    )

    result = runner.invoke(app, ["pr", "status", "--timeout", "540"])

    assert result.exit_code == 2
    assert "--timeout only works together with --wait" in _plain(result.output)


@pytest.mark.parametrize("bad", ["-1", "nan", "inf"])
def test_cli_rejects_a_timeout_that_is_not_a_finite_number(monkeypatch, bad) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.cli.current_remote", lambda: pytest.fail("must be rejected before anything runs")
    )

    result = runner.invoke(app, ["pr", "status", "--wait", "--timeout", bad])

    assert result.exit_code == 2


# -- an enclosing `timeout N` (as in `timeout 590 bdt pr status --wait`) ----------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["timeout", "590", "bdt", "pr", "status", "--wait"], 590),
        (["/usr/bin/timeout", "9m", "bdt"], 540),
        (["gtimeout", "1.5h", "bdt"], 5400),
        (["timeout", "-k", "10", "--signal=INT", "-v", "600", "bdt"], 600),
        (["timeout", "--kill-after", "10", "--foreground", "600s", "bdt"], 600),
        (["timeout", "-vk", "5", "600", "bdt"], 600),  # a flag cluster ending in -k takes the next argument
        (["timeout", "-vk5", "600", "bdt"], 600),
        (["timeout", "--kill", "5", "--sig", "KILL", "600", "bdt"], 600),  # abbreviated long options
        (["timeout", "--", "600", "bdt"], 600),
        (["timeout", "1e3", "bdt"], 1000),
        (["timeout", "-x", "600", "bdt"], None),  # unknown option: don't guess
        (["timeout", "inf", "bdt"], None),
        (["timeout", "0", "bdt"], None),  # 0 = no limit
        (["timeout", "soon", "bdt"], None),
        (["timeout"], None),
        (["bdt", "pr", "status", "--wait", "--timeout", "590"], None),
        ([], None),
    ],
)
def test_parse_timeout_command(args: list[str], expected: float | None) -> None:
    assert parse_timeout_command(args) == expected


def _fake_ancestors(monkeypatch, chain: list[tuple[int, float, list[str]]]) -> None:
    """The process tree above us, nearest parent first: (parent pid, seconds running, command line)."""
    by_pid = {100 + i: info for i, info in enumerate(chain)}
    monkeypatch.setattr("os.getppid", lambda: 100)
    monkeypatch.setattr("bmsdna.devtools.cli_tools._process_info", by_pid.get)


def test_remaining_time_of_the_enclosing_timeout_counts_what_already_ran(monkeypatch) -> None:
    # bdt <- timeout 590 (running for 3 s) <- bash
    _fake_ancestors(
        monkeypatch, [(101, 3.0, ["timeout", "590", "bdt", "pr", "status", "--wait"]), (1, 900.0, ["bash"])]
    )

    assert enclosing_timeout_remaining() == 587


def test_the_closest_of_several_nested_timeouts_wins(monkeypatch) -> None:
    _fake_ancestors(
        monkeypatch,
        [
            (101, 0.0, ["timeout", "590", "bdt"]),
            (102, 0.0, ["bash", "-c", "..."]),
            (1, 50.0, ["timeout", "300", "bash"]),
        ],
    )

    assert enclosing_timeout_remaining() == 250


def test_a_timeout_that_is_pid_1_is_found_too(monkeypatch) -> None:
    # `docker run image timeout 590 bdt pr status --wait`
    monkeypatch.setattr("os.getppid", lambda: 1)
    monkeypatch.setattr(
        "bmsdna.devtools.cli_tools._process_info", lambda pid: (0, 2.0, ["timeout", "590", "bdt"]) if pid == 1 else None
    )

    assert enclosing_timeout_remaining() == 588


def test_no_enclosing_timeout(monkeypatch) -> None:
    _fake_ancestors(monkeypatch, [(101, 5.0, ["bash"]), (1, 5.0, ["claude"])])

    assert enclosing_timeout_remaining() is None


def test_unreadable_process_tree_is_no_enclosing_timeout(monkeypatch) -> None:
    _fake_ancestors(monkeypatch, [])

    assert enclosing_timeout_remaining() is None


@pytest.mark.parametrize(
    ("enclosing", "own", "expected"),
    [
        (None, 0, 0),  # nothing around us: wait forever, as before
        (None, 120, 120),
        (590, 0, 590 - ENCLOSING_TIMEOUT_MARGIN_SECS),
        (590, 3600, 590 - ENCLOSING_TIMEOUT_MARGIN_SECS),
        (590, 120, 120),  # an explicit --timeout that fits stays
        (10, 0, 1),  # hardly any time left: still one status report
    ],
)
def test_limit_by_enclosing_timeout(monkeypatch, enclosing: float | None, own: float, expected: float) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli_tools.enclosing_timeout_remaining", lambda: enclosing)

    assert limit_by_enclosing_timeout(own) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [(["--wait"], 70), (["--wait", "--timeout", "30"], 30), (["--wait", "--timeout", "0"], 70), ([], 0)],
)
def test_pr_status_passes_the_limited_timeout_on(monkeypatch, args: list[str], expected: float) -> None:
    run = MagicMock()
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: AdoRemote("myorg", "MyProj", "myrepo"))
    monkeypatch.setattr("bmsdna.devtools.cli.pr_build.run", run)
    monkeypatch.setattr("bmsdna.devtools.cli_tools.enclosing_timeout_remaining", lambda: 100.0)

    result = runner.invoke(app, ["pr", "status", "--pr-id", "42", *args])

    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["timeout"] == expected


def test_a_real_timeout_process_is_found_above_us() -> None:
    if sys.platform == "win32" or shutil.which("timeout") is None or shutil.which("ps") is None:
        pytest.skip("needs coreutils timeout and ps")  # Windows' own timeout.exe is a different tool
    code = "from bmsdna.devtools.cli_tools import enclosing_timeout_remaining as r; print(r())"
    out = subprocess.run(
        ["timeout", "600", sys.executable, "-c", code], capture_output=True, encoding="utf-8", check=True
    ).stdout

    assert out.strip() != "None"
    assert 0 <= float(out) <= 600  # less if the test run itself is under a shorter `timeout`


def test_no_ps_means_no_enclosing_timeout(monkeypatch) -> None:
    """Windows has no `ps`: the detection quietly finds nothing instead of failing the command."""

    def no_ps(*args, **kwargs):
        raise FileNotFoundError("ps")

    monkeypatch.setattr("subprocess.run", no_ps)

    assert enclosing_timeout_remaining() is None


def test_rerun_command_without_a_timeout() -> None:
    assert status_rerun_cmd(42, 0) == "bdt pr status --pr-id 42 --wait"
    assert status_rerun_cmd(None, 0) == "bdt pr status --wait"
    assert status_rerun_cmd(42, 70) == "bdt pr status --pr-id 42 --wait --timeout 70"


# -- a stop signal (SIGTERM from `timeout`, `kill`, an agent harness) ---------------
# `signal.raise_signal` rather than `os.kill`: on Windows only the former runs the handler (the latter terminates us).


STOP = signal.SIGTERM


@pytest.fixture
def sigterm_during_sleep(monkeypatch) -> list[float]:
    """`time.sleep` that gets a SIGTERM instead of sleeping, once; records what it was asked for."""
    asked: list[float] = []

    def sleep(secs: float) -> None:
        asked.append(secs)
        if len(asked) == 1:
            signal.raise_signal(STOP)
        pytest.fail("kept sleeping after SIGTERM")

    monkeypatch.setattr(time, "sleep", sleep)
    return asked


def test_stop_signal_ends_the_sleep_and_expires_the_deadline(sigterm_during_sleep) -> None:
    before = signal.getsignal(STOP)
    with WaitDeadline(0, stop_on_signal=True) as deadline:
        assert signal.getsignal(STOP) != before
        assert not deadline.expired()

        deadline.sleep(30)

        assert deadline.expired() and deadline.stopped_by_signal
        assert signal.getsignal(STOP) == before  # a second signal terminates as usual
    assert sigterm_during_sleep == [30]


def test_stop_signal_outside_of_a_sleep_only_sets_the_flag(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    with WaitDeadline(0, stop_on_signal=True) as deadline:
        signal.raise_signal(STOP)  # e.g. in the middle of a request

        assert deadline.expired()
        deadline.sleep(30)

    assert slept == []


def test_previous_handler_is_put_back_on_exit() -> None:
    before = signal.getsignal(STOP)
    with WaitDeadline(0, stop_on_signal=True):
        pass

    assert signal.getsignal(STOP) == before


def test_no_handler_without_stop_on_signal() -> None:
    before = signal.getsignal(STOP)
    with WaitDeadline(0):
        assert signal.getsignal(STOP) == before


def test_stop_on_signal_off_the_main_thread_is_a_no_op() -> None:
    """`signal.signal` raises ValueError outside the main thread."""
    errors: list[BaseException] = []

    def target() -> None:
        try:
            with WaitDeadline(0, stop_on_signal=True) as deadline:
                assert not deadline.expired()
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=target)
    t.start()
    t.join()

    assert errors == []


def test_stop_on_signal_survives_an_os_that_refuses_the_handler(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise OSError("invalid argument")

    monkeypatch.setattr(signal, "signal", refuse)
    with WaitDeadline(0, stop_on_signal=True) as deadline:
        assert not deadline.expired()


def test_gh_wait_reports_and_exits_4_on_sigterm(monkeypatch, sigterm_during_sleep, capsys) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: _gh_pr([PASSED, PENDING]))

    with pytest.raises(SystemExit) as exc:
        gh_run(MagicMock(), wait=True)

    out = capsys.readouterr().out
    assert exc.value.code == EXIT_WAIT_TIMEOUT
    assert "[✓ PASS] CI / lint" in out and "[… PENDING] CI / build" in out
    assert out.rstrip().endswith(
        "Terminated while still running. Re-run `bdt pr status --pr-id 42 --wait` to keep waiting."
    )


def test_ado_wait_reports_and_exits_4_on_sigterm(monkeypatch, sigterm_during_sleep, capsys) -> None:
    _ado_env(monkeypatch, [{"id": 200, "status": "inProgress", "result": None, "definition": {"id": 2, "name": "ci"}}])

    with pytest.raises(SystemExit) as exc:
        ado_run(AdoRemote("myorg", "MyProj", "myrepo"), None, "main", wait=True, source_branch="feature-x")

    out = capsys.readouterr().out
    assert exc.value.code == EXIT_WAIT_TIMEOUT
    assert "build #200 inProgress" in out
    assert out.rstrip().endswith(
        "Terminated while still running. Re-run `bdt pr status --pr-id 42 --wait` to keep waiting."
    )


def test_status_without_wait_leaves_sigterm_alone(monkeypatch) -> None:
    """Nothing would ever look at the flag there, so a SIGTERM must keep killing the command."""
    seen: list[object] = []
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: seen.append(signal.getsignal(STOP)) or _gh_pr([PASSED])
    )
    monkeypatch.setattr("bmsdna.devtools.gh_pr.deploy_run_hint", lambda gh, base: None)
    before = signal.getsignal(STOP)

    gh_run(MagicMock(), wait=False)

    assert seen == [before]


def test_gh_wait_stops_on_sigterm_while_mergeability_is_unknown(monkeypatch, sigterm_during_sleep, capsys) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: {**_gh_pr([PENDING]), "mergeable": "UNKNOWN"}
    )

    with pytest.raises(SystemExit) as exc:
        gh_run(MagicMock(), wait=True)

    out = capsys.readouterr().out
    assert exc.value.code == EXIT_WAIT_TIMEOUT
    assert sigterm_during_sleep == [3]
    assert "mergeability not computed yet" in out
    assert out.rstrip().endswith(
        "Terminated while still running. Re-run `bdt pr status --pr-id 42 --wait` to keep waiting."
    )


def test_gh_wait_timeout_applies_while_mergeability_is_unknown(monkeypatch, sleeps, capsys) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: {**_gh_pr([PENDING]), "mergeable": "UNKNOWN"}
    )

    with pytest.raises(SystemExit) as exc:
        gh_run(MagicMock(), wait=True, timeout=10)

    assert exc.value.code == EXIT_WAIT_TIMEOUT
    assert sleeps == [3, 3, 3, 1]


def test_windows_sleeps_in_slices_so_a_signal_is_answered_promptly(monkeypatch) -> None:
    now = [0.0]
    slept: list[float] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda secs: (slept.append(secs), now.__setitem__(0, now[0] + secs))[0])

    WaitDeadline(0).sleep(2.5)

    assert slept == [1.0, 1.0, 0.5]


def test_windows_stops_slicing_once_signalled(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(sys, "platform", "win32")

    def sleep(secs: float) -> None:
        slept.append(secs)
        signal.raise_signal(STOP)  # the handler runs once the sleep returns

    monkeypatch.setattr(time, "sleep", sleep)
    with WaitDeadline(0, stop_on_signal=True) as deadline:
        deadline.sleep(30)

    assert slept == [1.0]
