"""`pr status --wait --timeout N`: give up after N seconds with the current state and exit code 4, so a caller
whose own time limit is shorter than CI (an agent's shell tool, say) gets a result instead of being cut off."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.cli_tools import ENCLOSING_TIMEOUT_MARGIN_SECS, EXIT_WAIT_TIMEOUT, WaitDeadline, enclosing_timeout_remaining, limit_by_enclosing_timeout, parse_timeout_command
from bmsdna.devtools.gh_pr import run as gh_run
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote
from bmsdna.devtools.issue_watch import EXIT_TIMEOUT
from bmsdna.devtools.pr_build import run as ado_run

runner = CliRunner()


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
    return {"number": 42, "title": "feat: widgets", "baseRefName": "main", "mergeable": "MERGEABLE", "isDraft": False, "statusCheckRollup": checks}


PENDING = {"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS", "workflowName": "CI"}
PASSED = {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS", "workflowName": "CI"}


def test_gh_wait_times_out_with_exit_4_and_reports_every_check(monkeypatch, sleeps, capsys) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: _gh_pr([PASSED, PENDING]))

    with pytest.raises(SystemExit) as exc:
        gh_run("gh", wait=True, timeout=70)

    out = capsys.readouterr().out
    assert exc.value.code == EXIT_WAIT_TIMEOUT
    assert sleeps == [30, 30, 10]
    assert "[✓ PASS] CI / lint" in out and "[… PENDING] CI / build" in out
    assert out.rstrip().endswith("Still running after 70 s. Re-run `bdt pr status --pr-id 42 --wait --timeout 70` to keep waiting.")


def test_gh_wait_without_timeout_keeps_polling(monkeypatch, sleeps) -> None:
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: _gh_pr([PENDING]))

    class Enough(Exception):
        pass

    def sleep_a_few_times(secs: float) -> None:
        sleeps.append(secs)
        if len(sleeps) > 200:
            raise Enough

    monkeypatch.setattr(time, "sleep", sleep_a_few_times)

    with pytest.raises(Enough):
        gh_run("gh", wait=True)

    assert len(sleeps) > 200


def test_gh_wait_returns_normally_when_checks_finish_before_the_deadline(monkeypatch, sleeps, capsys) -> None:
    polls = iter([_gh_pr([PENDING]), _gh_pr([{**PENDING, "status": "COMPLETED", "conclusion": "SUCCESS"}])])
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr", lambda gh, pr_id=None: next(polls))
    monkeypatch.setattr("bmsdna.devtools.gh_pr.deploy_run_hint", lambda gh, base: None)

    gh_run("gh", wait=True, timeout=300)

    assert sleeps == [30]
    assert "Still running" not in capsys.readouterr().out


# -- Azure DevOps ---------------------------------------------------------------


def _ado_env(monkeypatch, builds: list[dict]) -> None:
    monkeypatch.setattr("bmsdna.devtools.pr_build.requests.Session", lambda: MagicMock())
    monkeypatch.setattr("bmsdna.devtools.ado_auth.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda *a, **k: {"pullRequestId": 42, "title": "feat: widgets", "status": "active", "isDraft": False})
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_builds_for_pr", lambda *a, **k: builds)
    monkeypatch.setattr("bmsdna.devtools.pr_build.find_pending_approvals", lambda *a, **k: [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.print_build", lambda session, remote, b: print(f"  build #{b['id']} {b['status']}"))


def test_ado_wait_times_out_with_exit_4_and_reports_the_running_builds(monkeypatch, sleeps, capsys) -> None:
    _ado_env(monkeypatch, [{"id": 200, "status": "inProgress", "result": None, "definition": {"id": 2, "name": "ci"}}])

    with pytest.raises(SystemExit) as exc:
        ado_run(AdoRemote("myorg", "MyProj", "myrepo"), None, "main", wait=True, source_branch="feature-x", timeout=45)

    out = capsys.readouterr().out
    assert exc.value.code == EXIT_WAIT_TIMEOUT
    assert sleeps == [30, 15]
    assert "ci #200 inProgress" in out and "build #200 inProgress" in out
    assert out.rstrip().endswith("Still running after 45 s. Re-run `bdt pr status --pr-id 42 --wait --timeout 45` to keep waiting.")


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
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.run", lambda gh, wait, pr_id=None, timeout=0: seen.update(wait=wait, timeout=timeout))

    result = runner.invoke(app, ["pr", "status", "--wait", "--timeout", "540"])

    assert result.exit_code == 0, result.output
    assert seen == {"wait": True, "timeout": 540}


def _plain(output: str) -> str:
    """Rich colors and boxes the error panel on CI terminals; reduce it to the bare words."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", output)).split())


def test_cli_timeout_needs_wait(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: pytest.fail("must be rejected before anything runs"))

    result = runner.invoke(app, ["pr", "status", "--timeout", "540"])

    assert result.exit_code == 2
    assert "--timeout only works together with --wait" in _plain(result.output)


@pytest.mark.parametrize("bad", ["-1", "nan", "inf"])
def test_cli_rejects_a_timeout_that_is_not_a_finite_number(monkeypatch, bad) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: pytest.fail("must be rejected before anything runs"))

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
    monkeypatch.setattr("bmsdna.devtools.cli_tools._process_info", lambda pid: by_pid.get(pid))


def test_remaining_time_of_the_enclosing_timeout_counts_what_already_ran(monkeypatch) -> None:
    # bdt <- timeout 590 (running for 3 s) <- bash
    _fake_ancestors(monkeypatch, [(101, 3.0, ["timeout", "590", "bdt", "pr", "status", "--wait"]), (1, 900.0, ["bash"])])

    assert enclosing_timeout_remaining() == 587


def test_the_closest_of_several_nested_timeouts_wins(monkeypatch) -> None:
    _fake_ancestors(monkeypatch, [(101, 0.0, ["timeout", "590", "bdt"]), (102, 0.0, ["bash", "-c", "..."]), (1, 50.0, ["timeout", "300", "bash"])])

    assert enclosing_timeout_remaining() == 250


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


@pytest.mark.parametrize(("args", "expected"), [(["--wait"], 70), (["--wait", "--timeout", "30"], 30), (["--wait", "--timeout", "0"], 70), ([], 0)])
def test_pr_status_passes_the_limited_timeout_on(monkeypatch, args: list[str], expected: float) -> None:
    run = MagicMock()
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: AdoRemote("myorg", "MyProj", "myrepo"))
    monkeypatch.setattr("bmsdna.devtools.cli.pr_build.run", run)
    monkeypatch.setattr("bmsdna.devtools.cli_tools.enclosing_timeout_remaining", lambda: 100.0)

    result = runner.invoke(app, ["pr", "status", "--pr-id", "42", *args])

    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["timeout"] == expected


def test_a_real_timeout_process_is_found_above_us() -> None:
    if shutil.which("timeout") is None or shutil.which("ps") is None:
        pytest.skip("needs coreutils timeout and ps")
    code = "from bmsdna.devtools.cli_tools import enclosing_timeout_remaining as r; print(r())"
    out = subprocess.run(["timeout", "600", sys.executable, "-c", code], capture_output=True, encoding="utf-8", check=True).stdout

    assert 590 <= float(out) <= 600
