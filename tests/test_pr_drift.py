"""Target-branch drift of an Azure DevOps PR (`bdt pr status` / `bdt pr info`): real git repos for the merge, fakes for Azure DevOps."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from typer.testing import CliRunner

from bmsdna.devtools import pr_drift, pr_info
from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote
from bmsdna.devtools.pr_build import run, target_drift, target_drift_or_none, target_tip

REMOTE = AdoRemote(org="myorg", project="MyProj", repo="myrepo")
runner = CliRunner()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, encoding="utf-8"
    ).stdout.strip()


def commit(cwd: Path, name: str, content: str, message: str) -> str:
    (cwd / name).write_text(content)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


class Repo:
    """A clone of a bare `origin` whose `dev` branch has moved on from where the PR's source branch `feat` was cut."""

    def __init__(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(origin)], check=True)
        self.work = tmp_path / "work"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.work)], check=True, capture_output=True)
        git(self.work, "checkout", "-q", "-b", "dev")
        self.base = commit(self.work, "a.txt", "one\n", "base")
        git(self.work, "checkout", "-q", "-b", "feat")
        self.source = commit(self.work, "a.txt", "feature\n", "feat: edit a")
        git(self.work, "checkout", "-q", "dev")
        git(self.work, "push", "-q", "origin", "dev", "feat")
        # a clone that has neither: it has to fetch them by commit id, like a checkout of some other branch
        self.local = tmp_path / "local"
        subprocess.run(["git", "init", "-q", str(self.local)], check=True)
        git(self.local, "remote", "add", "origin", str(origin))

    def advance_dev(self, name: str, content: str) -> str:
        tip = commit(self.work, name, content, f"dev: {name}")
        git(self.work, "push", "-q", "origin", "dev")
        return tip

    def pr(self, **extra) -> dict:
        return {
            "pullRequestId": 7,
            "title": "feat: edit a",
            "status": "active",
            "targetRefName": "refs/heads/dev",
            "mergeStatus": "succeeded",
            "lastMergeTargetCommit": {"commitId": self.base},
            "lastMergeSourceCommit": {"commitId": self.source},
            **extra,
        }


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Repo:
    r = Repo(tmp_path)
    monkeypatch.chdir(r.local)
    monkeypatch.setattr(pr_drift, "current_remote", lambda cwd=None: REMOTE)
    return r


def test_merge_conflicts_lists_the_conflicting_paths(repo: Repo) -> None:
    tip = repo.advance_dev("a.txt", "dev\n")

    assert pr_drift.merge_conflicts(repo.source, tip, str(repo.work)) == ("a.txt",)


def test_merge_conflicts_empty_when_the_changes_do_not_overlap(repo: Repo) -> None:
    tip = repo.advance_dev("b.txt", "other\n")

    assert pr_drift.merge_conflicts(repo.source, tip, str(repo.work)) == ()


def test_merge_conflicts_unknown_when_a_commit_is_missing(repo: Repo) -> None:
    assert pr_drift.merge_conflicts("0" * 40, repo.base, str(repo.work)) is None


def test_check_is_none_while_the_target_has_not_moved(repo: Repo) -> None:
    assert pr_drift.check(REMOTE, repo.pr(), repo.base) is None


@pytest.mark.parametrize("pr_extra", [{"status": "completed"}, {"status": "abandoned"}, {"lastMergeTargetCommit": None}, {"lastMergeSourceCommit": None}])
def test_check_is_none_for_a_pr_that_has_nothing_to_merge_or_no_merge_data(repo: Repo, pr_extra: dict) -> None:
    tip = repo.advance_dev("a.txt", "dev\n")

    assert pr_drift.check(REMOTE, repo.pr(**pr_extra), tip) is None


def test_check_finds_the_conflict_azure_devops_does_not_report(repo: Repo) -> None:
    repo.advance_dev("b.txt", "unrelated\n")
    tip = repo.advance_dev("a.txt", "dev\n")

    drift = pr_drift.check(REMOTE, repo.pr(), tip)

    assert drift == pr_drift.Drift("dev", 2, ("a.txt",))
    assert drift.conflicting
    # the checkout got the commits but no branch or tag, and nothing was merged into it
    assert git(repo.local, "branch", "--list") == ""
    assert not (repo.local / "a.txt").exists()


def test_check_reports_a_clean_drift_as_behind(repo: Repo) -> None:
    tip = repo.advance_dev("b.txt", "unrelated\n")

    drift = pr_drift.check(REMOTE, repo.pr(), tip)

    assert drift == pr_drift.Drift("dev", 1, ())
    assert not drift.conflicting


def test_check_remembers_an_answer_per_commit(repo: Repo, monkeypatch) -> None:
    tip = repo.advance_dev("a.txt", "dev\n")
    assert pr_drift.check(REMOTE, repo.pr(), tip) == pr_drift.Drift("dev", 1, ("a.txt",))

    real_git = pr_drift._git

    def no_fetch_or_merge(args, *a, **k):
        assert args[0] not in ("fetch", "merge-tree"), "the same commits are not fetched and merged again"
        return real_git(args, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(pr_drift, "_git", no_fetch_or_merge)
        assert pr_drift.check(REMOTE, repo.pr(), tip) == pr_drift.Drift("dev", 1, ("a.txt",))

    newer = repo.advance_dev("c.txt", "more\n")
    assert pr_drift.check(REMOTE, repo.pr(), newer) == pr_drift.Drift("dev", 2, ("a.txt",))


def test_check_without_a_checkout_of_the_repo_only_knows_that_the_target_moved(repo: Repo, monkeypatch) -> None:
    tip = repo.advance_dev("a.txt", "dev\n")
    monkeypatch.setattr(pr_drift, "current_remote", lambda cwd=None: AdoRemote("myorg", "MyProj", "other"))

    assert pr_drift.check(REMOTE, repo.pr(), tip) == pr_drift.Drift("dev", None, None)


def test_check_does_not_remember_a_failed_fetch(repo: Repo) -> None:
    tip = repo.advance_dev("a.txt", "dev\n")
    git(repo.local, "remote", "set-url", "origin", str(repo.local / "nowhere"))

    assert pr_drift.check(REMOTE, repo.pr(), tip) == pr_drift.Drift("dev", None, None)

    git(repo.local, "remote", "set-url", "origin", str(repo.work.parent / "origin.git"))
    assert pr_drift.check(REMOTE, repo.pr(), tip) == pr_drift.Drift("dev", 1, ("a.txt",))


def test_messages_say_what_moved_and_what_to_do() -> None:
    pr = {"pullRequestId": 7, "title": "feat: x", "mergeStatus": "succeeded"}

    conflict = pr_drift.conflict_message(pr, pr_drift.Drift("dev", 3, ("a.txt", "b.txt")))
    assert "PR #7 ('feat: x') has merge conflicts with 'dev'" in conflict
    assert "stale" in conflict and "3 commits" in conflict and "a.txt, b.txt" in conflict
    assert "Merge or rebase 'dev'" in conflict

    assert "1 commit since" in pr_drift.notice(pr_drift.Drift("dev", 1, ()))
    assert "still merges cleanly" in pr_drift.notice(pr_drift.Drift("dev", 1, ()))
    assert "not checked for conflicts" in pr_drift.notice(pr_drift.Drift("dev", None, None))


def session_with_tip(tip: str | None, name: str = "refs/heads/dev") -> MagicMock:
    session = MagicMock()
    session.get.return_value.json.return_value = {"value": [{"name": "refs/heads/dev-old", "objectId": "x"}, *([{"name": name, "objectId": tip}] if tip else [])]}
    return session


def test_target_tip_picks_the_exact_branch() -> None:
    session = session_with_tip("abc")

    assert target_tip(session, REMOTE, "dev") == "abc"
    assert session.get.call_args.kwargs["params"]["filter"] == "heads/dev"
    assert target_tip(session_with_tip(None), REMOTE, "dev") is None


def test_target_drift_cannot_tell_when_azure_devops_cannot_be_asked(repo: Repo) -> None:
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("down")

    with pytest.raises(requests.ConnectionError):
        target_drift(session, REMOTE, repo.pr())
    assert target_drift_or_none(session, REMOTE, repo.pr()) is None


def _poll_once(monkeypatch, pr: dict) -> None:
    monkeypatch.setattr("bmsdna.devtools.pr_build.requests.Session", lambda: MagicMock())
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda session, remote, source, target: pr)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_builds_for_pr", lambda session, remote, source, pr_id: [])
    monkeypatch.setattr("bmsdna.devtools.pr_build.deploy_build_hint", lambda *a: None)


def test_status_exits_on_a_conflict_with_the_moved_target(repo: Repo, monkeypatch) -> None:
    pr = repo.pr()
    tip = repo.advance_dev("a.txt", "dev\n")
    _poll_once(monkeypatch, pr)
    monkeypatch.setattr("bmsdna.devtools.pr_build.target_tip", lambda *a: tip)

    with pytest.raises(SystemExit) as exc:
        run(REMOTE, "pat", "dev", wait=False, source_branch="feat")

    assert "PR #7" in str(exc.value) and "conflicts with 'dev'" in str(exc.value) and "a.txt" in str(exc.value)


def test_status_notes_a_moved_target_that_still_merges(repo: Repo, monkeypatch, capsys) -> None:
    pr = repo.pr()
    tip = repo.advance_dev("b.txt", "unrelated\n")
    _poll_once(monkeypatch, pr)
    monkeypatch.setattr("bmsdna.devtools.pr_build.target_tip", lambda *a: tip)

    run(REMOTE, "pat", "dev", wait=False, source_branch="feat")

    out = capsys.readouterr().out
    assert "Note: 'dev' has moved on by 1 commit since the PR's merge status and builds were computed; the PR still merges cleanly." in out
    assert "No builds found" in out


def test_wait_notices_a_target_that_moves_while_waiting(repo: Repo, monkeypatch, capsys) -> None:
    """Polls: the target is where the PR was merged, then somebody pushes a clean change, then a conflicting one."""
    pr = repo.pr()
    tips = iter([repo.base, repo.advance_dev("b.txt", "unrelated\n"), repo.advance_dev("a.txt", "dev\n")])
    _poll_once(monkeypatch, pr)
    monkeypatch.setattr("bmsdna.devtools.pr_build.target_tip", lambda *a: next(tips))
    monkeypatch.setattr("bmsdna.devtools.pr_build.lacks_build_policy", lambda *a: False)
    monkeypatch.setattr("bmsdna.devtools.pr_build.time.sleep", lambda s: None)

    with pytest.raises(SystemExit) as exc:
        run(REMOTE, "pat", "dev", wait=True, source_branch="feat")

    assert "conflicts with 'dev'" in str(exc.value)
    assert capsys.readouterr().out.count("the PR still merges cleanly") == 1


ADO_PR = {
    "pullRequestId": 42,
    "title": "feat: widgets",
    "status": "active",
    "isDraft": False,
    "sourceRefName": "refs/heads/feat",
    "targetRefName": "refs/heads/dev",
    "mergeStatus": "succeeded",
}


def _no_builds(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.get_builds_for_pr", lambda *a: [])


@pytest.mark.parametrize(
    "drift,expected",
    [
        (None, (pr_info.MERGE_CLEAN, [], None)),
        (pr_drift.Drift("dev", 2, ()), (pr_info.MERGE_BEHIND, [], 2)),
        (pr_drift.Drift("dev", 2, ("a.txt",)), (pr_info.MERGE_CONFLICTS, ["a.txt"], 2)),
        (pr_drift.Drift("dev", None, None), (pr_info.MERGE_UNKNOWN, [], None)),
    ],
)
def test_ado_info_merge_state_follows_the_drift(monkeypatch, drift, expected) -> None:
    _no_builds(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.target_drift", lambda *a: drift)

    info = pr_info.ado_info(None, ADO_PR, REMOTE)  # ty: ignore[invalid-argument-type]

    assert (info.merge, info.conflicts, info.target_behind) == expected
    assert info.target == "dev"


def test_ado_info_merge_is_unknown_when_the_target_cannot_be_looked_up(monkeypatch) -> None:
    _no_builds(monkeypatch)

    def down(*a):
        raise requests.ConnectionError("down")

    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.target_drift", down)

    assert pr_info.ado_info(None, ADO_PR, REMOTE).merge == pr_info.MERGE_UNKNOWN  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("merge_status", ["failure", "rejectedByPolicy"])
def test_ado_info_merge_is_unknown_not_conflicts_when_the_merge_failed_for_another_reason(monkeypatch, merge_status: str) -> None:
    _no_builds(monkeypatch)

    assert pr_info.ado_info(None, {**ADO_PR, "mergeStatus": merge_status}, REMOTE).merge == pr_info.MERGE_UNKNOWN  # ty: ignore[invalid-argument-type]


def test_ado_info_reports_the_conflicts_azure_devops_itself_found(monkeypatch) -> None:
    _no_builds(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.target_drift", lambda *a: pytest.fail("already known"))

    assert pr_info.ado_info(None, {**ADO_PR, "mergeStatus": "conflicts"}, REMOTE).merge == pr_info.MERGE_CONFLICTS  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("status", ["completed", "abandoned"])
def test_ado_info_has_no_merge_state_for_a_pr_that_is_not_open(monkeypatch, status: str) -> None:
    _no_builds(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.target_drift", lambda *a: pytest.fail("nothing to merge"))
    monkeypatch.setattr("bmsdna.devtools.pr_info.pr_build.abandonment", lambda *a: None)

    assert pr_info.ado_info(None, {**ADO_PR, "status": status}, REMOTE).merge == pr_info.MERGE_NONE  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "mergeable,state,expected",
    [("CONFLICTING", "OPEN", "conflicts"), ("MERGEABLE", "OPEN", "clean"), ("UNKNOWN", "OPEN", "clean"), ("CONFLICTING", "MERGED", "none")],
)
def test_github_info_merge_state_is_githubs_mergeable(mergeable: str, state: str, expected: str) -> None:
    pr = {"number": 1, "title": "t", "url": "u", "state": state, "baseRefName": "main", "mergeable": mergeable}

    info = pr_info.github_info(pr, GitHubRemote("owner", "repo"))

    assert (info.merge, info.target) == (expected, "main")


def _info(merge: str, **kw) -> pr_info.PrInfo:
    return pr_info.PrInfo(42, "feat: widgets", "https://x/42", "open", False, "passing", merge=merge, target="dev", **kw)


def test_info_text_lists_the_conflicts_and_the_drift() -> None:
    from bmsdna.devtools.cli import _info_text

    assert _info_text(_info("clean")) == "PR #42: feat: widgets (open, build: passing)\nhttps://x/42"
    assert _info_text(_info("conflicts", conflicts=["a.txt", "b.txt"], target_behind=3)).splitlines() == [
        "PR #42: feat: widgets (open, build: passing, merge: conflicts)",
        "https://x/42",
        "Conflicts with 'dev': a.txt, b.txt",
    ]
    assert _info_text(_info("behind", target_behind=1)).splitlines()[2] == "'dev' moved on by 1 commit since the PR's last merge check"


def test_info_json_carries_the_merge_state(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli._current_pr_info", lambda *a: _info("conflicts", conflicts=["a.txt"], target_behind=3))

    result = runner.invoke(app, ["pr", "info", "--json"])

    assert result.exit_code == 0
    import json

    data = json.loads(result.output)
    assert (data["merge"], data["target"], data["conflicts"], data["target_behind"]) == ("conflicts", "dev", ["a.txt"], 3)
