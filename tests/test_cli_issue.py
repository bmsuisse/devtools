"""`bdt issue search/create/update` at the CLI layer, covering the two changes from
GitHub issue #49:

1. `--org-wide` must be runnable outside of any git repo, by passing an explicit
   `--org` (Azure DevOps) or `--github-org` (GitHub) instead of relying on
   `current_remote()` (which shells out to `git remote get-url origin` and fails
   hard outside a repo, or in one with no matching remote). Crucially, --org/
   --github-org are only ever consulted as a *fallback* once `current_remote()`
   has actually failed -- never merely because one is set (they share envvars
   with the pre-existing `find-repo` command, which documents setting them
   persistently in a shell profile; gating on "is it set" instead of "did
   detecting the repo fail" would break an ordinary in-repo `issue search` for
   anyone who followed that advice).
2. `--tag` and `--label` (and `--remove-tag`/`--remove-label`) are aliases of each
   other -- each should route to whichever backend is actually active (Azure DevOps
   tags vs GitHub labels), instead of being silently ignored by the other backend.
"""

import re

from typer.testing import CliRunner

from bmsdna.devtools.cli import _merge_tags_and_labels, app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote, UnknownRemoteError

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI escape codes from Typer/Rich's error output before a substring check --
    whether Rich colors/bolds a `CliRunner` invocation's captured output (and where it splits
    escape codes across a flag name like `--org`) depends on env-var-driven color detection that
    differs between a local shell and CI, even though the underlying plain text is identical.
    """
    return _ANSI_RE.sub("", text)


def _not_a_repo():
    raise SystemExit("fatal: not a git repository (or any of the parent directories): .git")


# -- _merge_tags_and_labels ---------------------------------------------------


def test_merge_tags_and_labels_dedupes_case_insensitively_first_wins() -> None:
    assert _merge_tags_and_labels(["Bug", "urgent"], ["bug", "triage"]) == ["Bug", "urgent", "triage"]


def test_merge_tags_and_labels_none_when_both_none() -> None:
    assert _merge_tags_and_labels(None, None) is None


def test_merge_tags_and_labels_empty_list_when_both_empty() -> None:
    assert _merge_tags_and_labels([], []) == []


def test_merge_tags_and_labels_one_sided() -> None:
    assert _merge_tags_and_labels(["a"], None) == ["a"]
    assert _merge_tags_and_labels(None, ["b"]) == ["b"]


# -- issue search --org-wide outside a repo -----------------------------------


def test_issue_search_org_wide_falls_back_to_github_org_when_no_repo(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", _not_a_repo)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured: dict = {}

    def fake_search(gh, owner, repo, keywords, since, limit, state, board=None, org_wide=False, labels=None):
        captured.update(gh=gh, owner=owner, repo=repo, org_wide=org_wide, labels=labels)
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.search", fake_search)

    result = runner.invoke(app, ["issue", "search", "--org-wide", "--github-org", "bmsuisse"])

    assert result.exit_code == 0, result.output
    assert captured["owner"] == "bmsuisse"
    assert captured["org_wide"] is True


def test_issue_search_org_wide_falls_back_to_org_when_no_repo(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", _not_a_repo)
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    captured: dict = {}

    def fake_search(session, remote, keywords, since=None, board=None, top=10, state="open", org_wide=False, tags=None):
        captured.update(remote=remote, org_wide=org_wide, tags=tags)
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.search", fake_search)

    result = runner.invoke(app, ["issue", "search", "--org-wide", "--org", "bmeurope"])

    assert result.exit_code == 0, result.output
    assert isinstance(captured["remote"], AdoRemote)
    assert captured["remote"].org == "bmeurope"
    assert captured["org_wide"] is True


def test_issue_search_org_wide_falls_back_on_unrecognized_remote(monkeypatch) -> None:
    """A remote that's neither GitHub nor Azure DevOps (`UnknownRemoteError`) must fall back to
    --org/--github-org too, not just the "no git repo at all" (`SystemExit`) case.
    """

    def unrecognized_remote():
        raise UnknownRemoteError("not a supported remote")

    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", unrecognized_remote)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured: dict = {}

    def fake_search(gh, owner, repo, keywords, since, limit, state, board=None, org_wide=False, labels=None):
        captured["owner"] = owner
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.search", fake_search)

    result = runner.invoke(app, ["issue", "search", "--org-wide", "--github-org", "bmsuisse"])

    assert result.exit_code == 0, result.output
    assert captured["owner"] == "bmsuisse"


def test_issue_search_org_wide_without_org_flags_still_uses_current_remote(monkeypatch) -> None:
    """Unchanged default behavior: no --org/--github-org means --org-wide still needs to run
    from inside a repo, exactly like before this feature was added.
    """
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured: dict = {}

    def fake_search(gh, owner, repo, keywords, since, limit, state, board=None, org_wide=False, labels=None):
        captured.update(owner=owner, repo=repo)
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.search", fake_search)

    result = runner.invoke(app, ["issue", "search", "--org-wide"])

    assert result.exit_code == 0, result.output
    assert captured == {"owner": "owner", "repo": "repo"}


def test_issue_search_org_wide_without_repo_and_without_org_flags_still_errors(monkeypatch) -> None:
    """No --org/--github-org to fall back on: the original `current_remote()` error must still
    propagate unchanged (not silently swallowed into some other failure mode)."""
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", _not_a_repo)

    result = runner.invoke(app, ["issue", "search", "--org-wide"])

    assert result.exit_code != 0
    assert "not a git repository" in str(result.exception)


def test_issue_search_ignores_org_and_github_org_when_current_remote_succeeds(monkeypatch) -> None:
    """The core bug this design avoids: an ambient AZDO_ORG/GITHUB_ORG (e.g. set for `find-repo`,
    which documents having *both* set at once) must not affect a plain, in-repo `issue search` --
    --org/--github-org are only ever a fallback for when detecting the repo's own remote fails.
    """
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured: dict = {}

    def fake_search(gh, owner, repo, keywords, since, limit, state, board=None, org_wide=False, labels=None):
        captured.update(owner=owner, repo=repo)
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.search", fake_search)

    # Both --org and --github-org set simultaneously (as ambient envvars for `find-repo` might
    # be) would be an unresolvable conflict if actually used -- but since the repo's own remote
    # resolves fine here, neither should even be looked at.
    result = runner.invoke(
        app, ["issue", "search", "--org-wide", "--org", "bmeurope", "--github-org", "bmsuisse"]
    )

    assert result.exit_code == 0, result.output
    assert captured == {"owner": "owner", "repo": "repo"}


def test_issue_search_org_and_github_org_together_errors_only_when_actually_needed(monkeypatch) -> None:
    """Once `current_remote()` has actually failed and a fallback is genuinely needed, giving
    both --org and --github-org is an unresolvable conflict (org-wide search is single-backend)
    and should fail clearly, rather than silently picking one."""
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", _not_a_repo)

    result = runner.invoke(app, ["issue", "search", "--org-wide", "--org", "bmeurope", "--github-org", "bmsuisse"])

    assert result.exit_code != 0
    assert "only one of --org or --github-org" in _plain(result.output)


# -- --tag/--label aliasing ----------------------------------------------------


def test_issue_search_merges_tag_and_label_for_github(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured: dict = {}

    def fake_search(gh, owner, repo, keywords, since, limit, state, board=None, org_wide=False, labels=None):
        captured["labels"] = labels
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.search", fake_search)

    result = runner.invoke(app, ["issue", "search", "--tag", "bug", "--label", "urgent"])

    assert result.exit_code == 0, result.output
    assert captured["labels"] == ["bug", "urgent"]


def test_issue_search_merges_tag_and_label_for_ado(monkeypatch) -> None:
    remote = AdoRemote("myorg", "MyProj", "myrepo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    captured: dict = {}

    def fake_search(session, remote, keywords, since=None, board=None, top=10, state="open", org_wide=False, tags=None):
        captured["tags"] = tags
        return []

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.search", fake_search)

    result = runner.invoke(app, ["issue", "search", "--tag", "bug", "--label", "urgent"])

    assert result.exit_code == 0, result.output
    assert captured["tags"] == ["bug", "urgent"]


def test_issue_create_merges_tag_and_label_for_github(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured: dict = {}

    def fake_create(gh, owner, repo, title, description, labels, screenshot_paths, extra_args, file_paths=None, board=None):
        captured["labels"] = labels
        return None

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.create", fake_create)

    result = runner.invoke(app, ["issue", "create", "--title", "t", "--tag", "bug", "--label", "urgent"])

    assert result.exit_code == 0, result.output
    assert captured["labels"] == ["bug", "urgent"]


def test_issue_create_merges_tag_and_label_for_ado(monkeypatch) -> None:
    remote = AdoRemote("myorg", "MyProj", "myrepo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    captured: dict = {}

    def fake_create(session, remote, work_item_type, title, description, board, tags, screenshot_paths, file_paths):
        captured["tags"] = tags
        return {"id": 1}

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.create", fake_create)

    result = runner.invoke(app, ["issue", "create", "--title", "t", "--tag", "bug", "--label", "urgent"])

    assert result.exit_code == 0, result.output
    assert captured["tags"] == ["bug", "urgent"]


def test_issue_update_merges_tag_label_and_remove_tag_remove_label_for_github(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    captured: dict = {}

    def fake_update(gh, number, title, description, add_labels, remove_labels, state):
        captured.update(add_labels=add_labels, remove_labels=remove_labels)

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.update", fake_update)

    result = runner.invoke(
        app,
        ["issue", "update", "1", "--tag", "bug", "--label", "urgent", "--remove-tag", "wontfix", "--remove-label", "stale"],
    )

    assert result.exit_code == 0, result.output
    assert captured["add_labels"] == ["bug", "urgent"]
    assert captured["remove_labels"] == ["wontfix", "stale"]


def test_issue_update_merges_tag_label_and_remove_tag_remove_label_for_ado(monkeypatch) -> None:
    remote = AdoRemote("myorg", "MyProj", "myrepo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    captured: dict = {}

    def fake_update(session, remote, number, title, description, board, tags, remove_tags, state):
        captured.update(tags=tags, remove_tags=remove_tags)
        return {"id": number}

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.update", fake_update)

    result = runner.invoke(
        app,
        ["issue", "update", "1", "--tag", "bug", "--label", "urgent", "--remove-tag", "wontfix", "--remove-label", "stale"],
    )

    assert result.exit_code == 0, result.output
    assert captured["tags"] == ["bug", "urgent"]
    assert captured["remove_tags"] == ["wontfix", "stale"]


def test_issue_update_omitting_tag_and_label_leaves_ado_tags_untouched(monkeypatch) -> None:
    """Neither --tag nor --label given must forward `None` (leave unchanged), never `[]`
    (which Azure DevOps' `update()` would take as "replace with no tags")."""
    remote = AdoRemote("myorg", "MyProj", "myrepo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    captured: dict = {}

    def fake_update(session, remote, number, title, description, board, tags, remove_tags, state):
        captured.update(tags=tags, remove_tags=remove_tags)
        return {"id": number}

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.update", fake_update)

    result = runner.invoke(app, ["issue", "update", "1", "--title", "New title"])

    assert result.exit_code == 0, result.output
    assert captured["tags"] is None
    assert captured["remove_tags"] is None
