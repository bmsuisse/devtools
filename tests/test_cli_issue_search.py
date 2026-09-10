"""`bdt issue search --since-days` uses `0` as a documented sentinel meaning
"no date filter" (`since = ... if since_days > 0 else None`). Negative values
must be rejected rather than silently treated the same as `0` — otherwise a
caller (e.g. a script computing `since_days` from some other value) that
passes a negative number gets an unfiltered search instead of a clear error.
"""

from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import GitHubRemote

runner = CliRunner()


def test_issue_search_rejects_negative_since_days(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not be reached — negative --since-days must be rejected before searching")

    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", fail_if_called)

    result = runner.invoke(app, ["issue", "search", "foo", "--since-days", "-5"])

    assert result.exit_code != 0
    assert "--since-days" in result.output


def test_issue_search_since_days_zero_means_no_filter(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")

    captured: dict = {}

    def fake_search(gh, keywords, since, limit, state):
        captured["since"] = since
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.search", fake_search)

    result = runner.invoke(app, ["issue", "search", "foo", "--since-days", "0"])

    assert result.exit_code == 0, result.output
    assert captured["since"] is None


def test_issue_search_positive_since_days_computes_a_filter(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")

    captured: dict = {}

    def fake_search(gh, keywords, since, limit, state):
        captured["since"] = since
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.search", fake_search)

    result = runner.invoke(app, ["issue", "search", "foo", "--since-days", "7"])

    assert result.exit_code == 0, result.output
    assert captured["since"] is not None
