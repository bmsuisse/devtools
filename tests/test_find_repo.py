import subprocess

import pytest
import requests
from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.find_repo import RemoteRepo, find_github, find_local, find_remote, run, work_dir

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


# --- work_dir ------------------------------------------------------------


def test_work_dir_prefers_azdo_work_dir_over_bms_work_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AZDO_WORK_DIR", str(tmp_path / "azdo"))
    monkeypatch.setenv("BMS_WORK_DIR", str(tmp_path / "bms"))
    assert work_dir() == tmp_path / "azdo"


def test_work_dir_falls_back_to_bms_work_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZDO_WORK_DIR", raising=False)
    monkeypatch.setenv("BMS_WORK_DIR", str(tmp_path / "bms"))
    assert work_dir() == tmp_path / "bms"


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


def test_find_local_finds_repos_nested_under_a_project_folder(tmp_path) -> None:
    """A repo cloned by a previous `find-repo --yes` run lands at root/<project>/<repo>
    -- find_local (via worktree.find_repos' recursive walk) must still discover it."""
    repo = init_repo(tmp_path / "ProjectA" / "widgets")

    assert find_local("widgets", tmp_path) == [repo]


# --- find_remote --------------------------------------------------------------


class FakeResp:
    def __init__(self, status=200, body=None, reason="reason"):
        self.status_code = status
        self._body = body
        self.reason = reason

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _fake_ado(projects: list[dict], repos_by_project: dict[str, FakeResp | list[dict]], calls: list | None = None):
    """Fake `requests.get` for the Azure DevOps REST API, dispatching on the URL (not call
    order) so it stays correct under find_remote's concurrent per-project fan-out."""

    def fake_get(url, headers=None, params=None, timeout=None):
        if calls is not None:
            calls.append((url, headers, params))
        if url.endswith("/_apis/projects"):
            skip = params.get("$skip", 0)
            top = params.get("$top", 100)
            return FakeResp(body={"value": projects[skip : skip + top]})
        project = url.split("/")[-4]
        repos = repos_by_project.get(project, [])
        if isinstance(repos, FakeResp):
            return repos
        return FakeResp(body={"value": repos})

    return fake_get


@pytest.fixture(autouse=True)
def _ado_header(monkeypatch):
    monkeypatch.setattr("bmsdna.devtools.find_repo.auth_header", lambda pat: {"Authorization": f"pat:{pat}"})


def test_find_remote_prefers_exact_match_across_projects(monkeypatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        _fake_ado(
            [{"name": "ProjectA"}, {"name": "ProjectB"}],
            {
                "ProjectA": [{"name": "widgets-extra", "remoteUrl": "https://a/widgets-extra"}],
                "ProjectB": [{"name": "widgets", "remoteUrl": "https://b/widgets"}],
            },
        ),
    )

    result = find_remote("org", "widgets")

    assert result == [RemoteRepo("ProjectB", "widgets", "https://b/widgets")]


def test_find_remote_falls_back_to_substring_match(monkeypatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        _fake_ado(
            [{"name": "ProjectA"}],
            {"ProjectA": [{"name": "widgets-extra", "remoteUrl": "https://a/widgets-extra"}]},
        ),
    )

    result = find_remote("org", "widgets")

    assert result == [RemoteRepo("ProjectA", "widgets-extra", "https://a/widgets-extra")]


def test_find_remote_no_match(monkeypatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        _fake_ado([{"name": "ProjectA"}], {"ProjectA": [{"name": "other", "remoteUrl": "https://a/other"}]}),
    )

    assert find_remote("org", "widgets") == []


def test_find_remote_uses_pat_auth_and_pinned_api_version(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(requests, "get", _fake_ado([{"name": "ProjectA"}], {}, calls))

    find_remote("my org", "widgets", pat="secret")

    assert calls[0][0] == "https://dev.azure.com/my%20org/_apis/projects"
    assert all(c[1] == {"Authorization": "pat:secret"} for c in calls)
    assert all(c[2]["api-version"] == "7.1" for c in calls)


def test_find_remote_paginates_projects(monkeypatch) -> None:
    projects = [{"name": f"P{i}"} for i in range(250)]
    repos = {"P249": [{"name": "widgets", "remoteUrl": "https://p249/widgets"}]}
    monkeypatch.setattr(requests, "get", _fake_ado(projects, repos))

    assert find_remote("org", "widgets") == [RemoteRepo("P249", "widgets", "https://p249/widgets")]


def test_find_remote_exits_if_project_list_fails(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(401, {"message": "not logged in"}))

    with pytest.raises(SystemExit) as exc_info:
        find_remote("org", "widgets")

    assert "not logged in" in str(exc_info.value)


def test_find_remote_exits_if_credentials_rejected_with_html_page(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, None))

    with pytest.raises(SystemExit) as exc_info:
        find_remote("org", "widgets")

    assert "credentials rejected" in str(exc_info.value)


def test_find_remote_skips_project_whose_repos_list_fails_but_keeps_searching(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        _fake_ado(
            [{"name": "NoAccess"}, {"name": "ProjectB"}],
            {
                "NoAccess": FakeResp(403, {"message": "TF401019: no access"}),
                "ProjectB": [{"name": "widgets", "remoteUrl": "https://b/widgets"}],
            },
        ),
    )

    result = find_remote("org", "widgets")

    assert result == [RemoteRepo("ProjectB", "widgets", "https://b/widgets")]
    err = capsys.readouterr().err
    assert "NoAccess" in err and "TF401019" in err


# --- find_github --------------------------------------------------------------


class FakeGitHub:
    """Stands in for GitHubClient: `request` answers the org-vs-user probe, `paginate` the listing."""

    def __init__(self, repos: list[dict], org_status: int = 200, login: str = "someone-else"):
        self.repos = repos
        self.org_status = org_status
        self.login = login
        self.paginated: list[str] = []
        self.params: list[dict | None] = []
        self.requested: list[str] = []

    def get(self, path, **kwargs):
        assert path == "user"
        return {"login": self.login}

    def request(self, method, path, ok=(), **kwargs):
        assert method == "GET" and 404 in ok
        self.requested.append(path)
        return FakeResp(self.org_status if path.startswith("orgs/") else 200, [])

    def paginate(self, path, params=None, **kwargs):
        self.paginated.append(path)
        self.params.append(params)
        return self.repos


def test_find_github_prefers_exact_match() -> None:
    client = FakeGitHub(
        [
            {"name": "widgets-extra", "html_url": "https://github.com/org/widgets-extra"},
            {"name": "widgets", "html_url": "https://github.com/org/widgets"},
        ]
    )

    result = find_github(client, "org", "widgets")  # type: ignore[arg-type]

    assert result == [RemoteRepo("org", "widgets", "https://github.com/org/widgets", source="github")]
    assert client.paginated == ["orgs/org/repos"]


def test_find_github_falls_back_to_substring_match() -> None:
    client = FakeGitHub([{"name": "widgets-extra", "html_url": "https://github.com/org/widgets-extra"}])

    result = find_github(client, "org", "widgets")  # type: ignore[arg-type]

    assert result == [RemoteRepo("org", "widgets-extra", "https://github.com/org/widgets-extra", source="github")]


def test_find_github_no_match() -> None:
    client = FakeGitHub([{"name": "other", "html_url": "https://github.com/org/other"}])

    assert find_github(client, "org", "widgets") == []  # type: ignore[arg-type]


def test_find_github_falls_back_to_user_repos_on_404() -> None:
    client = FakeGitHub([{"name": "widgets", "html_url": "https://github.com/me/widgets"}], org_status=404)

    result = find_github(client, "me", "widgets")  # type: ignore[arg-type]

    assert client.paginated == ["users/me/repos"]
    assert result == [RemoteRepo("me", "widgets", "https://github.com/me/widgets", source="github")]


def test_find_github_lists_private_repos_for_the_authenticated_users_own_account() -> None:
    client = FakeGitHub([{"name": "secret", "html_url": "https://github.com/Me/secret"}], org_status=404, login="me")

    result = find_github(client, "Me", "secret")  # type: ignore[arg-type]

    assert (client.paginated, client.params) == (["user/repos"], [{"affiliation": "owner"}])
    assert result == [RemoteRepo("Me", "secret", "https://github.com/Me/secret", source="github")]


def test_find_github_quotes_the_org_into_a_single_path_segment() -> None:
    client = FakeGitHub([], org_status=404)

    find_github(client, "../../user", "x")  # type: ignore[arg-type]

    assert client.requested == ["orgs/..%2F..%2Fuser/repos"]
    assert client.paginated == ["users/..%2F..%2Fuser/repos"]


def test_find_github_propagates_client_errors() -> None:
    class Failing(FakeGitHub):
        def request(self, *a, **k):
            raise SystemExit("GitHub GET failed (HTTP 401): Bad credentials")

    with pytest.raises(SystemExit) as exc_info:
        find_github(Failing([]), "org", "widgets")  # type: ignore[arg-type]

    assert "Bad credentials" in str(exc_info.value)


def test_clone_github_uses_token_header_not_url_or_argv(tmp_path, monkeypatch) -> None:
    import base64

    from bmsdna.devtools.find_repo import clone_github

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    clone_github("org/widgets", tmp_path / "widgets", token="ghs_secret")

    assert captured["cmd"] == ["git", "clone", "https://github.com/org/widgets.git", str(tmp_path / "widgets")]
    assert "ghs_secret" not in " ".join(captured["cmd"])
    expected = base64.b64encode(b"x-access-token:ghs_secret").decode()
    assert captured["env"]["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: Basic {expected}"


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
    monkeypatch.setattr("bmsdna.devtools.find_repo.find_remote", lambda org, name, pat=None: [])

    with pytest.raises(SystemExit) as exc_info:
        run("widgets", root=tmp_path, org="someorg", yes=False)

    assert "widgets" in str(exc_info.value)


def test_run_exits_on_multiple_remote_matches(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda org, name, pat=None: [
            RemoteRepo("ProjectA", "widgets", "https://a/widgets"),
            RemoteRepo("ProjectB", "widgets", "https://b/widgets"),
        ],
    )
    clone_calls = []
    monkeypatch.setattr("bmsdna.devtools.find_repo.clone", lambda url, dest, auth=None: clone_calls.append((url, dest)))

    with pytest.raises(SystemExit):
        run("widgets", root=tmp_path, org="someorg", yes=True)

    assert clone_calls == []


def test_run_clones_into_project_nested_dest(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda org, name, pat=None: [RemoteRepo("ProjectA", "widgets", "https://a/widgets")],
    )
    monkeypatch.setattr("bmsdna.devtools.find_repo.auth_header", lambda pat: {"Authorization": "Bearer token"})
    clone_calls = []

    def fake_clone(url, dest, auth=None):
        clone_calls.append((url, dest, auth))
        return subprocess.CompletedProcess(["git", "clone"], 0)

    monkeypatch.setattr("bmsdna.devtools.find_repo.clone", fake_clone)

    run("widgets", root=tmp_path, org="someorg", yes=True)

    assert clone_calls == [("https://a/widgets", tmp_path / "ProjectA" / "widgets", {"Authorization": "Bearer token"})]


def test_run_exits_if_no_local_match_and_no_org_or_github_org(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run("widgets", root=tmp_path, org=None, github_org=None, yes=False)

    assert "AZDO_ORG" in str(exc_info.value)
    assert "GITHUB_ORG" in str(exc_info.value)


def test_run_clones_github_org_match_via_github_token_not_ado_auth(tmp_path, monkeypatch) -> None:
    """A GitHub-sourced match must clone via `clone_github` (GitHub token), never via
    `clone()` + `auth_header()` -- the latter is ADO's Basic-PAT-or-Bearer-token helper and
    must never be sent to github.com."""
    monkeypatch.setattr("bmsdna.devtools.find_repo.require_gh", lambda: "gh")
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_github",
        lambda gh, org, name: [RemoteRepo("someghorg", "widgets", "https://github.com/someghorg/widgets", source="github")],
    )
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.auth_header", lambda pat: (_ for _ in ()).throw(AssertionError("must not be called"))
    )
    clone_calls = []

    def fake_clone_github(full_name, dest):
        clone_calls.append((full_name, dest))
        return subprocess.CompletedProcess(["git", "clone"], 0)

    monkeypatch.setattr("bmsdna.devtools.find_repo.clone_github", fake_clone_github)

    run("widgets", root=tmp_path, org=None, github_org="someghorg", yes=True)

    assert clone_calls == [("someghorg/widgets", tmp_path / "github" / "widgets")]


def test_run_searches_both_org_and_github_org(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.find_repo.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.find_repo.find_remote", lambda org, name, pat=None: [])
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_github",
        lambda gh, org, name: [RemoteRepo("someghorg", "widgets", "https://github.com/someghorg/widgets", source="github")],
    )
    clone_calls = []
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.clone_github",
        lambda full_name, dest: clone_calls.append((full_name, dest)) or subprocess.CompletedProcess(["gh"], 0),
    )

    run("widgets", root=tmp_path, org="someorg", github_org="someghorg", yes=True)

    assert clone_calls == [("someghorg/widgets", tmp_path / "github" / "widgets")]


def test_run_prefers_exact_match_from_either_source_over_substring_from_the_other(tmp_path, monkeypatch) -> None:
    """An ADO substring-only match shouldn't beat an exact GitHub match (or vice versa) --
    exact-first must apply across the combined results, not just within each source."""
    monkeypatch.setattr("bmsdna.devtools.find_repo.require_gh", lambda: "gh")
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda org, name, pat=None: [RemoteRepo("ProjectA", "widgets-extra", "https://a/widgets-extra")],
    )
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_github",
        lambda gh, org, name: [RemoteRepo("someghorg", "widgets", "https://github.com/someghorg/widgets", source="github")],
    )
    clone_calls = []
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.clone_github",
        lambda full_name, dest: clone_calls.append((full_name, dest)) or subprocess.CompletedProcess(["gh"], 0),
    )

    run("widgets", root=tmp_path, org="someorg", github_org="someghorg", yes=True)

    assert clone_calls == [("someghorg/widgets", tmp_path / "github" / "widgets")]


def test_run_exits_if_dest_already_exists(tmp_path, monkeypatch) -> None:
    (tmp_path / "ProjectA" / "widgets").mkdir(parents=True)
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda org, name, pat=None: [RemoteRepo("ProjectA", "widgets", "https://a/widgets")],
    )
    clone_calls = []
    monkeypatch.setattr("bmsdna.devtools.find_repo.clone", lambda url, dest, auth=None: clone_calls.append((url, dest)))

    with pytest.raises(SystemExit) as exc_info:
        run("widgets", root=tmp_path, org="someorg", yes=True)

    assert "already exists" in str(exc_info.value)
    assert clone_calls == []


def test_run_prompts_before_cloning_and_skips_on_no(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda org, name, pat=None: [RemoteRepo("ProjectA", "widgets", "https://a/widgets")],
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    clone_calls = []
    monkeypatch.setattr("bmsdna.devtools.find_repo.clone", lambda url, dest, auth=None: clone_calls.append((url, dest)))

    run("widgets", root=tmp_path, org="someorg", yes=False)

    assert clone_calls == []


def test_run_skips_prompt_and_does_not_clone_when_stdin_is_not_a_tty(tmp_path, monkeypatch, capsys) -> None:
    """Non-interactive callers (CI, an AI-agent caller, piped/closed stdin) must never
    block on input() -- they get told to pass --yes instead."""
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda org, name, pat=None: [RemoteRepo("ProjectA", "widgets", "https://a/widgets")],
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def fail_if_called(prompt):
        raise AssertionError("must not call input() when stdin is not a tty")

    monkeypatch.setattr("builtins.input", fail_if_called)
    clone_calls = []
    monkeypatch.setattr("bmsdna.devtools.find_repo.clone", lambda url, dest, auth=None: clone_calls.append((url, dest)))

    run("widgets", root=tmp_path, org="someorg", yes=False)

    assert clone_calls == []
    assert "--yes" in capsys.readouterr().out


def test_run_clones_remote_only_match_when_yes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda org, name, pat=None: [RemoteRepo("ProjectA", "widgets", "https://a/widgets")],
    )
    monkeypatch.setattr("bmsdna.devtools.find_repo.auth_header", lambda pat: {"Authorization": "Bearer token"})
    clone_calls = []

    def fake_clone(url, dest, auth=None):
        clone_calls.append((url, dest, auth))
        return subprocess.CompletedProcess(["git", "clone"], 0)

    monkeypatch.setattr("bmsdna.devtools.find_repo.clone", fake_clone)

    run("widgets", root=tmp_path, org="someorg", yes=True)

    assert clone_calls == [("https://a/widgets", tmp_path / "ProjectA" / "widgets", {"Authorization": "Bearer token"})]


def test_run_passes_pat_through_to_auth_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda org, name, pat=None: [RemoteRepo("ProjectA", "widgets", "https://a/widgets")],
    )
    auth_calls = []
    monkeypatch.setattr("bmsdna.devtools.find_repo.auth_header", lambda pat: auth_calls.append(pat) or {})
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.clone", lambda url, dest, auth=None: subprocess.CompletedProcess(["git"], 0)
    )

    run("widgets", root=tmp_path, org="someorg", yes=True, pat="my-pat")

    assert auth_calls == ["my-pat"]


def test_run_raises_on_failed_clone(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.find_remote",
        lambda org, name, pat=None: [RemoteRepo("ProjectA", "widgets", "https://a/widgets")],
    )
    monkeypatch.setattr("bmsdna.devtools.find_repo.auth_header", lambda pat: {})
    monkeypatch.setattr(
        "bmsdna.devtools.find_repo.clone", lambda url, dest, auth=None: subprocess.CompletedProcess(["git", "clone"], 1)
    )

    with pytest.raises(SystemExit) as exc_info:
        run("widgets", root=tmp_path, org="someorg", yes=True)

    assert exc_info.value.code == 1


# --- clone -----------------------------------------------------------------------


def test_clone_passes_auth_header_via_env_not_argv(tmp_path, monkeypatch) -> None:
    """The PAT must never appear in argv (visible via `ps aux`) -- only via env vars."""
    from bmsdna.devtools.find_repo import clone

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    clone("https://example/widgets", tmp_path / "widgets", {"Authorization": "Bearer secret-token"})

    assert "secret-token" not in " ".join(captured["cmd"])
    assert captured["env"]["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert captured["env"]["GIT_CONFIG_VALUE_0"] == "AUTHORIZATION: Bearer secret-token"


def test_clone_exits_cleanly_when_git_missing(tmp_path, monkeypatch) -> None:
    from bmsdna.devtools.find_repo import clone

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        clone("https://example/widgets", tmp_path / "widgets")

    assert "git" in str(exc_info.value)


# --- CLI wiring ----------------------------------------------------------------


def test_find_repo_cli_delegates_to_run(monkeypatch, tmp_path) -> None:
    # Cleared explicitly (not just relying on the environment): a PAT set in the
    # ambient shell (e.g. AZURE_DEVOPS_EXT_PAT from a dev machine's bashrc) would
    # otherwise leak into `--pat`'s envvar fallback and make this assertion flaky.
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    monkeypatch.delenv("GITHUB_ORG", raising=False)
    monkeypatch.delenv("BMS_GITHUB_ORG", raising=False)
    captured: dict = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.find_repo_mod.run",
        lambda name, **kwargs: captured.update(name=name, **kwargs),
    )

    result = runner.invoke(app, ["find-repo", "widgets", "--root", str(tmp_path), "--org", "someorg", "--yes"])

    assert result.exit_code == 0, result.output
    assert captured == {
        "name": "widgets",
        "root": tmp_path,
        "org": "someorg",
        "github_org": None,
        "yes": True,
        "pat": None,
    }


def test_find_repo_cli_passes_github_org(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    captured: dict = {}
    monkeypatch.setattr(
        "bmsdna.devtools.cli.find_repo_mod.run",
        lambda name, **kwargs: captured.update(name=name, **kwargs),
    )

    result = runner.invoke(app, ["find-repo", "widgets", "--root", str(tmp_path), "--github-org", "someghorg", "--yes"])

    assert result.exit_code == 0, result.output
    assert captured["github_org"] == "someghorg"


def test_clone_appends_to_existing_git_config_env(tmp_path, monkeypatch) -> None:
    from bmsdna.devtools.find_repo import clone

    captured: dict = {}
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.sslCAInfo")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/etc/ca.pem")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: captured.update(env=kw["env"]) or subprocess.CompletedProcess(cmd, 0))

    clone("https://example/x.git", tmp_path / "x", {"Authorization": "Basic abc"})

    env = captured["env"]
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("http.sslCAInfo", "/etc/ca.pem")
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("http.extraheader", "AUTHORIZATION: Basic abc")
