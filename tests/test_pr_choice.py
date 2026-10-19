"""Finding "the PR opened from the current branch" without `--target`: a PR into any branch (not just
`dev`) is found; exactly one open PR is used silently; several open PRs are asked about on a terminal
and are an error (listing them) when non-interactive -- the same for Azure DevOps and GitHub, and for
every command that does this lookup.
"""

import pytest
from typer.testing import CliRunner

from bmsdna.devtools import gh_pr, pr_build, pr_choice
from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote

runner = CliRunner()

ADO_REMOTE = AdoRemote("myorg", "MyProj", "myrepo")
GITHUB_REMOTE = GitHubRemote("owner", "repo")

ADO_INTO_MAIN = {"pullRequestId": 10, "title": "into main", "targetRefName": "refs/heads/main", "mergeStatus": "succeeded"}
ADO_INTO_DEV = {"pullRequestId": 11, "title": "into dev", "targetRefName": "refs/heads/dev", "mergeStatus": "succeeded"}
# GitHub REST `pulls` objects
GH_INTO_MAIN = {"number": 10, "title": "into main", "base": {"ref": "main"}, "html_url": "https://github.com/owner/repo/pull/10", "state": "open"}
GH_INTO_DEV = {"number": 11, "title": "into dev", "base": {"ref": "dev"}, "html_url": "https://github.com/owner/repo/pull/11", "state": "open"}


class PrListClient:
    """Stands in for `GitHubClient`: answers every `pulls` lookup with `prs`, recording the query params."""

    owner = "owner"
    repo = "repo"
    repo_path = "repos/owner/repo"

    def __init__(self, prs: list[dict]) -> None:
        self.prs = prs
        self.calls: list[dict] = []

    def get(self, path, params=None, **kwargs):
        assert path == "repos/owner/repo/pulls"
        self.calls.append(params or {})
        return self.prs


@pytest.fixture
def interactive(monkeypatch):
    monkeypatch.setattr(pr_choice, "_interactive", lambda: True)


@pytest.fixture
def non_interactive(monkeypatch):
    monkeypatch.setattr(pr_choice, "_interactive", lambda: False)
    monkeypatch.setattr(pr_choice.typer, "prompt", lambda *a, **k: pytest.fail("must not prompt when non-interactive"))


@pytest.fixture
def never_asks(monkeypatch):
    monkeypatch.setattr(pr_choice.typer, "prompt", lambda *a, **k: pytest.fail("must not ask when there is nothing to choose"))


def _assert_lists_both_and_says_how_to_choose(message: str) -> None:
    for text in ("#10", "main", "into main", "#11", "dev", "into dev", "--pr-id", "--target"):
        assert text in message


class AdoSession:
    """Answers the pull request search by `searchCriteria.status`; records the params of each call."""

    def __init__(self, active: list[dict], completed: list[dict] | None = None) -> None:
        self.by_status = {"active": active, "completed": completed or []}
        self.params: list[dict] = []
        self.headers: dict = {}

    def get(self, url, params, timeout):
        self.params.append(params)
        value = self.by_status[params["searchCriteria.status"]]

        class Resp:
            def raise_for_status(self): ...
            def json(self):
                return {"value": value}

        return Resp()


# -- Azure DevOps ---------------------------------------------------------------


def test_ado_finds_a_pr_into_main_without_target_and_does_not_ask(never_asks) -> None:
    session = AdoSession([ADO_INTO_MAIN])

    pr = pr_build.get_pr(session, ADO_REMOTE, "feat", None)  # ty: ignore[invalid-argument-type]

    assert pr["pullRequestId"] == 10
    assert "searchCriteria.targetRefName" not in session.params[0]


def test_ado_fetches_all_active_prs_of_the_branch_not_just_the_first(never_asks) -> None:
    session = AdoSession([ADO_INTO_MAIN])

    pr_build.get_pr(session, ADO_REMOTE, "feat", None)  # ty: ignore[invalid-argument-type]

    assert "$top" not in session.params[0]


def test_ado_several_open_prs_interactive_asks_and_uses_the_chosen_one(interactive, monkeypatch, capsys) -> None:
    prompts: list[str] = []
    monkeypatch.setattr(pr_choice.typer, "prompt", lambda text, **k: prompts.append(text) or 2)
    session = AdoSession([ADO_INTO_MAIN, ADO_INTO_DEV])

    pr = pr_build.get_pr(session, ADO_REMOTE, "feat", None)  # ty: ignore[invalid-argument-type]

    assert pr["pullRequestId"] == 11
    assert len(prompts) == 1
    shown = capsys.readouterr().err
    assert "#10" in shown and "#11" in shown and "main" in shown and "dev" in shown
    assert "/pullrequest/10" in shown  # the PR's web url, to tell them apart


def test_ado_several_open_prs_reasks_on_an_out_of_range_answer(interactive, monkeypatch) -> None:
    answers = iter([0, 5, 1])
    monkeypatch.setattr(pr_choice.typer, "prompt", lambda text, **k: next(answers))

    pr = pr_build.get_pr(AdoSession([ADO_INTO_MAIN, ADO_INTO_DEV]), ADO_REMOTE, "feat", None)  # ty: ignore[invalid-argument-type]

    assert pr["pullRequestId"] == 10


def test_ado_several_open_prs_non_interactive_exits_listing_them(non_interactive) -> None:
    with pytest.raises(SystemExit) as exc_info:
        pr_build.get_pr(AdoSession([ADO_INTO_MAIN, ADO_INTO_DEV]), ADO_REMOTE, "feat", None)  # ty: ignore[invalid-argument-type]

    assert isinstance(exc_info.value.code, str)  # a message, i.e. non-zero
    _assert_lists_both_and_says_how_to_choose(exc_info.value.code)


def test_ado_explicit_target_filters_and_never_asks(never_asks) -> None:
    session = AdoSession([ADO_INTO_DEV])

    pr = pr_build.get_pr(session, ADO_REMOTE, "feat", "dev")  # ty: ignore[invalid-argument-type]

    assert pr["pullRequestId"] == 11
    assert session.params[0]["searchCriteria.targetRefName"] == "refs/heads/dev"


def test_ado_completed_prs_are_not_candidates(never_asks) -> None:
    """No active PR: falls back to the latest completed one, which is never a choice between several."""
    session = AdoSession([], completed=[{**ADO_INTO_MAIN, "status": "completed"}])

    assert pr_build.get_pr(session, ADO_REMOTE, "feat", None)["pullRequestId"] == 10  # ty: ignore[invalid-argument-type]
    assert [p["searchCriteria.status"] for p in session.params] == ["active", "completed"]
    assert session.params[1]["$top"] == 1


# -- GitHub ---------------------------------------------------------------------


@pytest.fixture
def gh_list(monkeypatch):
    """A client whose `pulls` lookup answers `prs`; `install` returns it (its `calls` are the recorded query params)."""

    def install(prs: list[dict]) -> PrListClient:
        monkeypatch.setattr(gh_pr, "current_branch", lambda: "feat")
        return PrListClient(prs)

    return install


def test_gh_pr_id_is_used_as_is_without_looking_anything_up(gh_list, never_asks) -> None:
    client = gh_list([GH_INTO_MAIN, GH_INTO_DEV])

    assert gh_pr.resolve_pr_id(client, 99) == 99  # ty: ignore[invalid-argument-type]
    assert client.calls == []


def test_gh_single_open_pr_into_main_is_used_without_asking(gh_list, never_asks) -> None:
    client = gh_list([GH_INTO_MAIN])

    assert gh_pr.resolve_pr_id(client, None) == 10  # ty: ignore[invalid-argument-type]
    assert client.calls[0]["head"] == "owner:feat" and client.calls[0]["state"] == "open"  # only open PRs are candidates
    assert "base" not in client.calls[0]


def test_gh_no_open_pr_leaves_it_to_gh(gh_list, never_asks) -> None:
    """E.g. the PR was already merged: `gh pr view` without a number still finds it, as before."""
    client = gh_list([])

    assert gh_pr.resolve_pr_id(client, None) is None  # ty: ignore[invalid-argument-type]


def test_gh_target_narrows_the_search_to_that_base(gh_list, never_asks) -> None:
    client = gh_list([GH_INTO_DEV])

    assert gh_pr.resolve_pr_id(client, None, "dev") == 11  # ty: ignore[invalid-argument-type]
    assert client.calls[0]["base"] == "dev"


def test_gh_target_without_a_matching_open_pr_exits(gh_list, never_asks) -> None:
    client = gh_list([])

    with pytest.raises(SystemExit) as exc_info:
        gh_pr.resolve_pr_id(client, None, "release")  # ty: ignore[invalid-argument-type]

    assert "'feat'" in str(exc_info.value) and "'release'" in str(exc_info.value)


def test_gh_several_open_prs_interactive_asks_and_uses_the_chosen_one(gh_list, interactive, monkeypatch, capsys) -> None:
    client = gh_list([GH_INTO_MAIN, GH_INTO_DEV])
    monkeypatch.setattr(pr_choice.typer, "prompt", lambda text, **k: 2)

    assert gh_pr.resolve_pr_id(client, None) == 11  # ty: ignore[invalid-argument-type]
    shown = capsys.readouterr().err
    assert "#10" in shown and "#11" in shown and "https://github.com/owner/repo/pull/11" in shown


def test_gh_several_open_prs_non_interactive_exits_listing_them(gh_list, non_interactive) -> None:
    client = gh_list([GH_INTO_MAIN, GH_INTO_DEV])

    with pytest.raises(SystemExit) as exc_info:
        gh_pr.resolve_pr_id(client, None)  # ty: ignore[invalid-argument-type]

    assert isinstance(exc_info.value.code, str)
    _assert_lists_both_and_says_how_to_choose(exc_info.value.code)


# -- through the CLI: commands that are not publish/update/comment/retry ----------


def _github_cli(monkeypatch, prs: list[dict]) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GITHUB_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: PrListClient(prs))
    monkeypatch.setattr(gh_pr, "current_branch", lambda: "feat")


def test_cli_pr_status_non_interactive_with_several_prs_fails_without_acting(monkeypatch) -> None:
    _github_cli(monkeypatch, [GH_INTO_MAIN, GH_INTO_DEV])
    monkeypatch.setattr(pr_choice, "_interactive", lambda: False)
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.run", lambda *a, **k: pytest.fail("must not pick a PR on its own"))

    result = runner.invoke(app, ["pr", "status"])

    assert result.exit_code != 0
    _assert_lists_both_and_says_how_to_choose(result.output)


def test_cli_pr_status_interactive_with_several_prs_acts_on_the_chosen_one(monkeypatch) -> None:
    _github_cli(monkeypatch, [GH_INTO_MAIN, GH_INTO_DEV])
    monkeypatch.setattr(pr_choice, "_interactive", lambda: True)
    seen = {}
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.run", lambda gh, wait, pr_id=None, timeout=0: seen.update(pr_id=pr_id))

    result = runner.invoke(app, ["pr", "status"], input="1\n")

    assert result.exit_code == 0, result.output
    assert seen == {"pr_id": 10}


def test_cli_pr_status_with_a_single_pr_into_main_does_not_ask(monkeypatch, never_asks) -> None:
    _github_cli(monkeypatch, [GH_INTO_MAIN])
    seen = {}
    monkeypatch.setattr("bmsdna.devtools.cli.gh_pr.run", lambda gh, wait, pr_id=None, timeout=0: seen.update(pr_id=pr_id))

    result = runner.invoke(app, ["pr", "status"])

    assert result.exit_code == 0, result.output
    assert seen == {"pr_id": 10}


def test_cli_issue_take_non_interactive_with_several_prs_fails_without_commenting(monkeypatch) -> None:
    _github_cli(monkeypatch, [GH_INTO_MAIN, GH_INTO_DEV])
    monkeypatch.setattr(pr_choice, "_interactive", lambda: False)
    monkeypatch.setattr("bmsdna.devtools.cli._current_user", lambda remote: "octocat")
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.comment", lambda *a, **k: pytest.fail("must not comment"))

    result = runner.invoke(app, ["issue", "take"])

    assert result.exit_code != 0
    _assert_lists_both_and_says_how_to_choose(result.output)


def test_cli_pr_publish_on_azure_devops_with_several_prs_non_interactive_fails(monkeypatch) -> None:
    session = AdoSession([ADO_INTO_MAIN, ADO_INTO_DEV])
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feat")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.cli.requests.Session", lambda: session)
    monkeypatch.setattr(pr_choice, "_interactive", lambda: False)
    monkeypatch.setattr("bmsdna.devtools.cli.pr_build.publish", lambda *a, **k: pytest.fail("must not pick a PR on its own"))

    result = runner.invoke(app, ["pr", "publish"])

    assert result.exit_code != 0
    _assert_lists_both_and_says_how_to_choose(result.output)


def test_cli_pr_publish_on_azure_devops_with_target_picks_that_pr(monkeypatch, never_asks) -> None:
    session = AdoSession([ADO_INTO_DEV])
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO_REMOTE)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feat")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.cli.requests.Session", lambda: session)
    published = []
    monkeypatch.setattr("bmsdna.devtools.cli.pr_build.publish", lambda s, remote, pr: published.append(pr["pullRequestId"]))

    result = runner.invoke(app, ["pr", "publish", "--target", "dev"])

    assert result.exit_code == 0, result.output
    assert published == [11]
    assert session.params[0]["searchCriteria.targetRefName"] == "refs/heads/dev"

