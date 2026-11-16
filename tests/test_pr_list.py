"""`bdt pr list`: id, state, name and tags of the repo's PRs, on GitHub and Azure DevOps."""

import json

from fake_github import FakeGitHub
from typer.testing import CliRunner

from bmsdna.devtools import pr_list
from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote

runner = CliRunner()

GITHUB_REMOTE = GitHubRemote("owner", "repo")
ADO_REMOTE = AdoRemote("myorg", "MyProj", "myrepo")


def _gh_pr(number: int, title: str, state: str = "open", labels: tuple[str, ...] = (), **extra) -> dict:
    return {
        "number": number,
        "title": title,
        "state": state,
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "labels": [{"name": n} for n in labels],
        **extra,
    }


class _Response:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


class _Session:
    def __init__(self, prs: list[dict]) -> None:
        self.prs = prs
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict, timeout: float) -> _Response:
        self.calls.append((url, params))
        return _Response({"value": self.prs})


def test_github_list_has_id_state_name_and_tags() -> None:
    gh = FakeGitHub(
        {
            "GET repos/owner/repo/pulls": [
                _gh_pr(5, "feat: widgets", labels=("bug", "test-full")),
                _gh_pr(4, "wip", draft=True),
            ]
        }
    )

    prs = pr_list.github_list(gh, "open", 10)  # ty: ignore[invalid-argument-type]

    assert [pr_list.summary_text(p) for p in prs] == [
        "#5  open  feat: widgets  [bug, test-full]",
        "#4  open (draft)  wip",
    ]
    assert gh.calls[0][2] == {"params": {"state": "open", "sort": "created", "direction": "desc"}, "limit": 10}


def test_github_list_tells_merged_from_closed() -> None:
    gh = FakeGitHub(
        {
            "GET repos/owner/repo/pulls": [
                _gh_pr(3, "merged one", "closed", merged_at="2026-01-01T00:00:00Z"),
                _gh_pr(2, "dropped one", "closed", merged_at=None),
            ]
        }
    )

    assert [p.state for p in pr_list.github_list(gh, "all")] == ["merged", "closed"]  # ty: ignore[invalid-argument-type]


def test_github_list_merged_goes_through_search() -> None:
    hit = _gh_pr(3, "merged one", "closed", pull_request={"merged_at": "2026-01-01T00:00:00Z"})
    gh = FakeGitHub({"GET search/issues": [hit]})

    prs = pr_list.github_list(gh, "merged")  # ty: ignore[invalid-argument-type]

    assert [(p.number, p.state) for p in prs] == [(3, "merged")]
    query = gh.calls[0][2]["params"]["q"]
    assert "repo:owner/repo" in query and "is:pr" in query and "is:merged" in query


def test_ado_list_maps_status_and_skips_removed_tags() -> None:
    session = _Session(
        [
            {
                "pullRequestId": 41,
                "title": "old",
                "status": "completed",
                "labels": [{"name": "bug", "active": True}, {"name": "gone", "active": False}],
            },
            {"pullRequestId": 42, "title": "new", "status": "active", "isDraft": True, "labels": []},
        ]
    )

    prs = pr_list.ado_list(session, ADO_REMOTE, "all", 5)  # ty: ignore[invalid-argument-type]

    assert [pr_list.summary_text(p) for p in prs] == ["#42  open (draft)  new", "#41  merged  old  [bug]"]
    assert prs[0].url == "https://dev.azure.com/myorg/MyProj/_git/myrepo/pullrequest/42"
    assert session.calls[0][1]["searchCriteria.status"] == "all" and session.calls[0][1]["$top"] == 5


def test_ado_list_open_asks_for_active() -> None:
    session = _Session([])

    assert pr_list.ado_list(session, ADO_REMOTE) == []  # ty: ignore[invalid-argument-type]
    assert session.calls[0][1]["searchCriteria.status"] == "active"


def test_pr_list_cli_github_text_and_json(monkeypatch) -> None:
    gh = FakeGitHub({"GET repos/owner/repo/pulls": [_gh_pr(5, "feat: widgets", labels=("bug",))]})
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: gh)

    text = runner.invoke(app, ["pr", "list"])
    as_json = runner.invoke(app, ["pr", "list", "--json"])

    assert text.exit_code == 0, text.output
    assert text.output == "#5  open  feat: widgets  [bug]\n"
    assert json.loads(as_json.output) == [
        {
            "number": 5,
            "title": "feat: widgets",
            "state": "open",
            "draft": False,
            "labels": ["bug"],
            "url": "https://github.com/owner/repo/pull/5",
        }
    ]


def test_pr_list_cli_ado(monkeypatch) -> None:
    session = _Session([{"pullRequestId": 7, "title": "fix", "status": "abandoned", "labels": [{"name": "chore"}]}])
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.ado_session", lambda pat: session)

    result = runner.invoke(app, ["pr", "list", "--state", "closed"])

    assert result.exit_code == 0, result.output
    assert result.output == "#7  closed  fix  [chore]\n"
    assert session.calls[0][1]["searchCriteria.status"] == "abandoned"


def test_pr_list_cli_empty_json_is_a_list(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: FakeGitHub({"GET repos/owner/repo/pulls": []}))

    result = runner.invoke(app, ["pr", "list", "--json"])

    assert result.exit_code == 0 and json.loads(result.output) == []


def test_pr_list_cli_rejects_unknown_state(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)

    assert runner.invoke(app, ["pr", "list", "--state", "bogus"]).exit_code == 2


def test_ado_list_quotes_the_repo_name() -> None:
    session = _Session([])

    pr_list.ado_list(session, AdoRemote("myorg", "MyProj", "lib%20core"))  # ty: ignore[invalid-argument-type]

    assert session.calls[0][0].endswith("/repositories/lib%2520core/pullrequests")
