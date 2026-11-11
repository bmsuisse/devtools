import subprocess

import pytest
import requests
from typer.testing import CliRunner

from bmsdna.devtools import ci_plan_env
from bmsdna.devtools.ci_plan import BranchFlowError
from bmsdna.devtools.cli import app

ADO_ENV = {
    "TF_BUILD": "True",
    "SYSTEM_COLLECTIONURI": "https://dev.azure.com/org/",
    "SYSTEM_TEAMPROJECTID": "proj",
    "BUILD_REPOSITORY_ID": "repo",
    "SYSTEM_ACCESSTOKEN": "tok",
}
PR_ENV = {
    **ADO_ENV,
    "BUILD_REASON": "PullRequest",
    "BUILD_SOURCEBRANCH": "refs/pull/7/merge",
    "SYSTEM_PULLREQUEST_PULLREQUESTID": "7",
    "SYSTEM_PULLREQUEST_SOURCEBRANCH": "refs/heads/feat/x",
    "SYSTEM_PULLREQUEST_TARGETBRANCH": "refs/heads/dev",
    "SYSTEM_PULLREQUEST_ISFORK": "False",
}


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload, self.status = payload, status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise requests.HTTPError(f"HTTP {self.status}")

    def json(self) -> object:
        return self.payload


@pytest.fixture
def api(monkeypatch):
    """Records requests; `api.routes[url-suffix] = FakeResponse`."""
    calls: list[tuple[str, tuple]] = []
    routes: dict[str, FakeResponse] = {}

    def fake_get(url, auth=None, timeout=None):
        calls.append((url, auth))
        for suffix, response in routes.items():
            if url.split("?")[0].endswith(suffix):
                return response
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(ci_plan_env.requests, "get", fake_get)
    fake_get.calls, fake_get.routes = calls, routes
    return fake_get


# --- labels and context ------------------------------------------------------------------------------------------


def test_parse_labels_keeps_active_names_only() -> None:
    payload = {
        "value": [
            {"name": "test-orders", "active": True},
            {"name": "old", "active": False},
            {"name": "x"},
        ]
    }
    assert ci_plan_env.parse_labels(payload) == ["test-orders", "x"]


@pytest.mark.parametrize(
    "payload", [None, {}, {"value": "x"}, {"value": [{"nom": "a"}]}, {"value": [3]}]
)
def test_parse_labels_rejects_unexpected_shapes(payload) -> None:
    with pytest.raises(ValueError):
        ci_plan_env.parse_labels(payload)


def test_fetch_labels_calls_the_pr_labels_api_with_the_build_token(api) -> None:
    api.routes["pullrequests/7/labels"] = FakeResponse(
        {"value": [{"name": "test-full"}]}
    )
    assert ci_plan_env.fetch_labels(PR_ENV, 7) == ["test-full"]
    url, auth = api.calls[0]
    assert (
        url
        == "https://dev.azure.com/org/proj/_apis/git/repositories/repo/pullrequests/7/labels?api-version=7.1"
    )
    assert auth == ("", "tok")


def test_fetch_labels_without_token_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="SYSTEM_ACCESSTOKEN"):
        ci_plan_env.fetch_labels(
            {k: v for k, v in PR_ENV.items() if k != "SYSTEM_ACCESSTOKEN"}, 7
        )


def test_context_for_pr_validation_build() -> None:
    ctx = ci_plan_env.context_from_env(PR_ENV)
    assert (ctx.pr_id, ctx.source, ctx.target, ctx.is_fork) == (
        7,
        "feat/x",
        "dev",
        False,
    )


def test_context_for_branch_build() -> None:
    ctx = ci_plan_env.context_from_env(
        {"BUILD_REASON": "Schedule", "BUILD_SOURCEBRANCH": "refs/heads/dev"}
    )
    assert (ctx.pr_id, ctx.branch, ctx.reason) == (None, "dev", "Schedule")


def test_manual_run_of_a_pr_merge_ref_is_planned_like_that_pr(api) -> None:
    api.routes["pullrequests/9"] = FakeResponse(
        {"sourceRefName": "refs/heads/feat/y", "targetRefName": "refs/heads/dev"}
    )
    ctx = ci_plan_env.context_from_env(
        {**ADO_ENV, "BUILD_REASON": "Manual", "BUILD_SOURCEBRANCH": "refs/pull/9/merge"}
    )
    assert (ctx.pr_id, ctx.source, ctx.target) == (9, "feat/y", "dev")


def test_manual_run_of_a_fork_pr_merge_ref_keeps_the_fork_flag(api) -> None:
    api.routes["pullrequests/9"] = FakeResponse(
        {
            "sourceRefName": "refs/heads/feat/y",
            "targetRefName": "refs/heads/dev",
            "forkSource": {"name": "x"},
        }
    )
    ctx = ci_plan_env.context_from_env(
        {**ADO_ENV, "BUILD_REASON": "Manual", "BUILD_SOURCEBRANCH": "refs/pull/9/merge"}
    )
    assert ctx.is_fork


# --- git -------------------------------------------------------------------------------------------------------


def git(cwd, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={**__import__("os").environ, **env},
    )
    return done.stdout.strip()


def commit(cwd, name: str) -> None:
    (cwd / name).write_text(name)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", name)


@pytest.fixture
def clone(tmp_path):
    """origin with main (m1) and dev (m1 + d1); a clone with origin/main and origin/dev."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    commit(seed, "m1")
    git(seed, "checkout", "-q", "-b", "dev")
    commit(seed, "d1")
    git(seed, "checkout", "-q", "main")
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    return work


def merge_fix_into_main(work, base: str, *, main_moves: bool = False) -> None:
    """HEAD becomes the PR merge commit of a fix branch cut from `base` (parent 1 = main, parent 2 = the fix)."""
    git(work, "checkout", "-q", "-b", "fix/x", base)
    commit(work, "f1")
    git(work, "checkout", "-q", "main")
    if main_moves:
        commit(work, "m2")
        git(work, "push", "-q", "origin", "main")
    git(work, "merge", "-q", "--no-ff", "-m", "merge", "fix/x")


def test_fix_cut_from_main_passes(clone, monkeypatch) -> None:
    monkeypatch.chdir(clone)
    merge_fix_into_main(clone, "origin/main")
    ci_plan_env.verify_fix_base({})


def test_fix_cut_from_dev_is_rejected(clone, monkeypatch) -> None:
    monkeypatch.chdir(clone)
    merge_fix_into_main(clone, "origin/dev")
    with pytest.raises(BranchFlowError, match="dev commits"):
        ci_plan_env.verify_fix_base({})


def test_fix_cut_from_main_stays_valid_when_main_moves_on(clone, monkeypatch) -> None:
    monkeypatch.chdir(clone)
    merge_fix_into_main(clone, "origin/main", main_moves=True)
    ci_plan_env.verify_fix_base({})


def test_fix_check_needs_a_merge_commit(clone, monkeypatch) -> None:
    monkeypatch.chdir(clone)
    with pytest.raises(RuntimeError, match="not a PR merge commit"):
        ci_plan_env.verify_fix_base({})


def test_changed_files_in_ci_use_the_first_parent_and_locally_origin_target(
    clone, monkeypatch
) -> None:
    monkeypatch.chdir(clone)
    git(clone, "checkout", "-q", "-b", "feat")
    commit(clone, "a.txt")
    assert ci_plan_env.changed_files("main", merge_checkout=False) == ["a.txt"]
    git(clone, "checkout", "-q", "main")
    git(clone, "merge", "-q", "--no-ff", "-m", "merge", "feat")
    assert ci_plan_env.changed_files("main", merge_checkout=True) == ["a.txt"]


def test_local_dry_run_does_not_mistake_an_ordinary_merge_for_the_pr_merge(
    clone, monkeypatch
) -> None:
    """feature changes code, then merges main (docs only): the local diff must still see the code change."""
    monkeypatch.chdir(clone)
    git(clone, "checkout", "-q", "-b", "feat")
    commit(clone, "code.py")
    git(clone, "checkout", "-q", "main")
    commit(clone, "README.md")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "checkout", "-q", "feat")
    git(clone, "merge", "-q", "-m", "sync main", "main")
    assert "code.py" in ci_plan_env.changed_files("main", merge_checkout=False)


def test_fix_check_in_a_shallow_checkout_only_warns(
    tmp_path, clone, monkeypatch
) -> None:
    shallow = tmp_path / "shallow"
    git(
        tmp_path,
        "clone",
        "-q",
        "--depth",
        "1",
        f"file://{tmp_path / 'origin.git'}",
        str(shallow),
    )
    monkeypatch.chdir(shallow)
    merge_fix_into_main(shallow, "origin/main")
    with pytest.raises(RuntimeError, match="shallow checkout"):
        ci_plan_env.verify_fix_base({})


# --- the command ---------------------------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "tests/unit/orders").mkdir(parents=True)
    (tmp_path / "tests/unit/orders/test_a.py").write_text("")
    (tmp_path / "pyproject.toml").write_text("[tool.bdt.ci.plan]\n")
    monkeypatch.chdir(tmp_path)
    for key in (*PR_ENV, "BUILD_REASON"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_plan_in_azure_pipelines_sets_output_variables(
    project, monkeypatch, api, capsys
) -> None:
    for key, value in PR_ENV.items():
        monkeypatch.setenv(key, value)
    api.routes["pullrequests/7/labels"] = FakeResponse(
        {"value": [{"name": "test-orders"}]}
    )
    result = CliRunner().invoke(app, ["ci", "plan"])
    assert result.exit_code == 0, result.output
    assert "test plan: areas (PR labels select orders)" in result.output
    assert (
        "##vso[task.setvariable variable=runTests;isOutput=true]true" in result.output
    )
    assert (
        "##vso[task.setvariable variable=unitPaths;isOutput=true]tests/unit/orders"
        in result.output
    )


def test_plan_branch_flow_violation_fails_the_build(project, monkeypatch, api) -> None:
    for key, value in {
        **PR_ENV,
        "SYSTEM_PULLREQUEST_TARGETBRANCH": "refs/heads/main",
    }.items():
        monkeypatch.setenv(key, value)
    result = CliRunner().invoke(app, ["ci", "plan"])
    assert result.exit_code == 1
    assert "##vso[task.logissue type=error]branch flow violated" in result.output


def test_plan_label_api_outage_runs_everything(project, monkeypatch, api) -> None:
    for key, value in PR_ENV.items():
        monkeypatch.setenv(key, value)
    result = CliRunner().invoke(app, ["ci", "plan"])
    assert result.exit_code == 0
    assert (
        "test plan: full" in result.output
        and "runTests;isOutput=true]true" in result.output
    )


def test_dry_run_prints_plain_variables(project) -> None:
    result = CliRunner().invoke(
        app, ["ci", "plan", "--pr-source", "dev", "--pr-target", "test"]
    )
    assert result.exit_code == 0
    assert "runTests=false" in result.output and "##vso" not in result.output


def test_dry_run_with_labels(project) -> None:
    result = CliRunner().invoke(
        app,
        [
            "ci",
            "plan",
            "--pr-source",
            "feat/x",
            "--pr-target",
            "dev",
            "--label",
            "test-full",
        ],
    )
    assert "unitPaths=tests/unit" in result.output


def test_source_without_target_is_a_usage_error(project) -> None:
    assert CliRunner().invoke(app, ["ci", "plan", "--pr-source", "x"]).exit_code == 2


def test_missing_config_table_is_an_error(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["ci", "plan"])
    assert result.exit_code == 2 and "tool.bdt.ci.plan" in result.output


def test_invalid_config_is_reported_not_raised(project) -> None:
    (project / "pyproject.toml").write_text('[tool.bdt.ci.plan]\non_missing = "x"\n')
    result = CliRunner().invoke(app, ["ci", "plan"])
    assert result.exit_code == 2 and "on_missing" in result.output


def test_vso_commands_cannot_be_split_by_untrusted_text(capsys) -> None:
    ci_plan_env.log(True, "warning", "bad\n##vso[task.complete result=Succeeded]")
    ci_plan_env.emit(True, "unitPaths", "a\n##vso[task.setvariable variable=x]y 100%")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert "%0A" in lines[0] and "%0A" in lines[1] and "100%AZP25" in lines[1]


def test_changed_files_lists_a_moved_file_under_its_old_path_too(
    clone, monkeypatch
) -> None:
    """Rename detection would show only docs/app.py: code moved into a non_code folder must not look like a docs change."""
    monkeypatch.chdir(clone)
    (clone / "src").mkdir()
    (clone / "docs").mkdir()
    (clone / "src/app.py").write_text("def f():\n    return 1\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "app")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "checkout", "-q", "-b", "move")
    git(clone, "mv", "src/app.py", "docs/app.py")
    git(clone, "commit", "-q", "-m", "move")
    assert sorted(ci_plan_env.changed_files("main", merge_checkout=False)) == [
        "docs/app.py",
        "src/app.py",
    ]
    git(clone, "checkout", "-q", "main")
    git(clone, "merge", "-q", "--no-ff", "-m", "merge", "move")
    assert sorted(ci_plan_env.changed_files("main", merge_checkout=True)) == [
        "docs/app.py",
        "src/app.py",
    ]
