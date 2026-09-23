import json
import subprocess

import pytest
from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.find_repo import RemoteRepo, find_local, find_remote, resolve_org, run, work_dir

runner = CliRunner()


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    (path / "README.md").write_text("init")
    _git(["add", "."], cwd=path)
    _git(["commit", "-q", "-m", "init"], cwd=path)
    return path


# --- work_dir / resolve_org -------------------------------------------------


def test_work_dir_prefers_azdo_work_dir_over_bms_work_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AZDO_WORK_DIR", str(tmp_path / "azdo"))
    monkeypatch.setenv("BMS_WORK_DIR", str(tmp_path / "bms"))
    assert work_dir() == tmp_path / "azdo"


def test_work_dir_falls_back_to_bms_work_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZDO_WORK_DIR", raising=False)
    monkeypatch.setenv("BMS_WORK_DIR", str(tmp_path / "bms"))
    assert work_dir() == tmp_path / "bms"


def test_resolve_org_prefers_cli_arg(monkeypatch) -> None:
    monkeypatch.setenv("AZDO_ORG", "envorg")
    assert resolve_org("cliorg") == "cliorg"


def test_resolve_org_falls_back_to_env(monkeypatch) -> None:
    monkeypatch.delenv("AZDO_ORG", raising=False)
    monkeypatch.setenv("BMS_ORG", "bmsorg")
    assert resolve_org(None) == "bmsorg"


def test_resolve_org_none_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("AZDO_ORG", raising=False)
    monkeypatch.delenv("BMS_ORG", raising=False)
    assert resolve_org(None) is None


# --- find_local --------------------------------------------------------------


def test_find_local_prefers_exact_match_over_substring(tmp_path) -> None:
    exact = init_repo(tmp_path / "widgets")
    init_repo(tmp_path / "widgets-extra")

    assert find_local("widgets", tmp_path) == [exact]


def test_find_local_falls_back_to_substring_match(tmp_path) -> None:
    extra = init_repo(tmp_path / "widgets-extra")

    assert find_local("widgets", tmp_path) == [extra]


def test_find_local_is_case_insensitive(tmp_path) -> None:
    repo = init_repo(tmp_path / "Widgets")

    assert find_local("widgets", tmp_path) == [repo]


def test_find_local_no_match(tmp_path) -> None:
    init_repo(tmp_path / "other")

    assert find_local("widgets", tmp_path) == []


# --- find_remote --------------------------------------------------------------


def _fake_az(projects_by_call: list[dict]):
    """Fake `az ... -o json` runner: first call returns the project list, each
    following call returns that project's repo list, in `projects_by_call` order."""
    calls = iter(projects_by_call)

    def fake_run(cmd, **kwargs):
        payload = next(calls)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    return fake_run


def test_find_remote_prefers_exact_match_across_projects(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_az(
            [
                {"value": [{"name": "ProjectA"}, {"name": "ProjectB"}]},
                [{"name": "widgets-extra", "remoteUrl": "https://a/widgets-extra"}],
                [{"name": "widgets", "remoteUrl": "https://b/widgets"}],
            ]
        ),
    )

    result = find_remote("az", "org", "widgets")

    assert result == [RemoteRepo("ProjectB", "widgets", "https://b/widgets")]


def test_find_remote_falls_back_to_substring_match(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_az(
            [
                {"value": [{"name": "ProjectA"}]},
                [{"name": "widgets-extra", "remoteUrl": "https://a/widgets-extra"}],
            ]
        ),
    )

    result = find_remote("az", "org", "widgets")

    assert result == [RemoteRepo("ProjectA", "widgets-extra", "https://a/widgets-extra")]


def test_find_remote_no_match(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_az(
            [
                {"value": [{"name": "ProjectA"}]},
                [{"name": "other", "remoteUrl": "https://a/other"}],
            ]
        ),
    )

    assert find_remote("az", "org", "widgets") == []


# --- run -----------------------------------------------------------------------


def test_run_exits_if_work_dir_missing(tmp_path, capsys) -> None:
    missing = tmp_path / "does-not-exist"

    with pytest.raises(SystemExit) as exc_info:
        run("widgets", root=missing, org=None, yes=False)

    assert "AZDO_WORK_DIR" in str(exc_info.value)


def test_run_prints_local_match_without_touching_remote(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "widgets")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not search remote when a local match exists")

    monkeypatch.setattr("bmsdna.devtools.find_repo.find_remote", fail_if_called)

    run("widgets", root=tmp_path, org="someorg", yes=False)

    assert str(repo) in capsys.readouterr().out


def test_run_exits_if_no_local_match_and_no_org(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run("widgets", root=tmp_path, org=None, yes=False)

    assert "AZDO_ORG" in str(exc_info.value)


def test_run_exits_if_no_match_anywhere(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.find_repo.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.find_repo.find_remote", lambda az, org, name: [])

    with pytest.raises(SystemExit) as exc_info:
        run("widgets", root=tmp_path, org="someorg", yes=False)

    assert "widgets" in str(exc_info.value)


def test_run_exits_on_multiple_remote_matches(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.find_repo.require_az", lambda: "az")
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda az, org, name: [
            RemoteRepo("ProjectA", "widgets", "https://a/widgets"),
            RemoteRepo("ProjectB", "widgets", "https://b/widgets"),
        ],
    )
    clone_calls = []
    monkeypatch.setattr("bmsdna.devtools.find_repo.clone", lambda url, dest: clone_calls.append((url, dest)))

    with pytest.raises(SystemExit):
        run("widgets", root=tmp_path, org="someorg", yes=True)

    assert clone_calls == []


def test_run_prompts_before_cloning_and_skips_on_no(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.find_repo.require_az", lambda: "az")
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda az, org, name: [RemoteRepo("ProjectA", "widgets", "https://a/widgets")],
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    clone_calls = []
    monkeypatch.setattr("bmsdna.devtools.find_repo.clone", lambda url, dest: clone_calls.append((url, dest)))

    run("widgets", root=tmp_path, org="someorg", yes=False)

    assert clone_calls == []


def test_run_clones_remote_only_match_when_yes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.find_repo.require_az", lambda: "az")
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda az, org, name: [RemoteRepo("ProjectA", "widgets", "https://a/widgets")],
    )
    clone_calls = []

    def fake_clone(url, dest):
        clone_calls.append((url, dest))
        return subprocess.CompletedProcess(["git", "clone"], 0)

    monkeypatch.setattr("bmsdna.devtools.find_repo.clone", fake_clone)

    run("widgets", root=tmp_path, org="someorg", yes=True)

    assert clone_calls == [("https://a/widgets", tmp_path / "widgets")]


def test_run_raises_on_failed_clone(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.find_repo.require_az", lambda: "az")
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda az, org, name: [RemoteRepo("ProjectA", "widgets", "https://a/widgets")],
    )
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.clone", lambda url, dest: subprocess.CompletedProcess(["git", "clone"], 1)
    )

    with pytest.raises(SystemExit) as exc_info:
        run("widgets", root=tmp_path, org="someorg", yes=True)

    assert exc_info.value.code == 1


# --- CLI wiring ----------------------------------------------------------------


def test_find_repo_cli_delegates_to_run(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.find_repo_mod.run",
        lambda name, **kwargs: captured.update(name=name, **kwargs),
    )

    result = runner.invoke(app, ["find-repo", "widgets", "--root", str(tmp_path), "--org", "someorg", "--yes"])

    assert result.exit_code == 0, result.output
    assert captured == {"name": "widgets", "root": tmp_path, "org": "someorg", "yes": True}
