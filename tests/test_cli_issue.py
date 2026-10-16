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

import json
import re

import pytest
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

    def fake_create(gh, owner, repo, title, description, labels, screenshot_paths, extra_args, file_paths=None, board=None, parent=None, blocked_by=None):
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

    def fake_create(session, remote, work_item_type, title, description, board, tags, screenshot_paths, file_paths, iteration=None, parent=None, blocked_by=None):
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

    def fake_update(gh, number, title, description, add_labels, remove_labels, state, parent=None, blocked_by=None):
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

    def fake_update(session, remote, number, title, description, board, tags, remove_tags, state, iteration=None, parent=None, blocked_by=None):
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

    def fake_update(session, remote, number, title, description, board, tags, remove_tags, state, iteration=None, parent=None, blocked_by=None):
        captured.update(tags=tags, remove_tags=remove_tags)
        return {"id": number}

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.update", fake_update)

    result = runner.invoke(app, ["issue", "update", "1", "--title", "New title"])

    assert result.exit_code == 0, result.output
    assert captured["tags"] is None
    assert captured["remove_tags"] is None


def _ado_remote_with_config(monkeypatch, tmp_path, config: str) -> None:
    (tmp_path / "pyproject.toml").write_text(config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: AdoRemote("myorg", "MyProj", "myrepo"))
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})


def test_issue_create_ado_fails_without_board_when_required(monkeypatch, tmp_path) -> None:
    _ado_remote_with_config(monkeypatch, tmp_path, "[tool.bdt.ado]\nrequire_board = true\n")
    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.create", lambda *a, **k: pytest.fail("must not create"))

    result = runner.invoke(app, ["issue", "create", "--title", "t"])

    assert result.exit_code != 0


def test_issue_create_ado_uses_canonical_allowed_board(monkeypatch, tmp_path) -> None:
    _ado_remote_with_config(monkeypatch, tmp_path, "[tool.bdt.ado]\nrequire_board = true\nallowed_boards = ['Data Team']\n")
    captured: dict = {}

    def fake_create(session, remote, work_item_type, title, description, board, tags, screenshot_paths, file_paths, iteration=None, parent=None, blocked_by=None):
        captured["board"] = board
        return {"id": 1}

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.create", fake_create)

    result = runner.invoke(app, ["issue", "create", "--title", "t", "--board", "data team"])

    assert result.exit_code == 0, result.output
    assert captured["board"] == "Data Team"


def test_issue_update_ado_rejects_disallowed_board(monkeypatch, tmp_path) -> None:
    _ado_remote_with_config(monkeypatch, tmp_path, "[tool.bdt.ado]\nallowed_boards = ['Data Team']\n")
    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.update", lambda *a, **k: pytest.fail("must not update"))

    result = runner.invoke(app, ["issue", "update", "1", "--board", "Other Team"])

    assert result.exit_code != 0


# -- issue show / --iteration / --parent / --blocked-by -----------------------


def _github(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GitHubRemote("owner", "repo"))
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")


def test_issue_show_github_prints_text_and_json(monkeypatch) -> None:
    from bmsdna.devtools.issue_show import IssueDetails

    _github(monkeypatch)
    details = IssueDetails(number=7, title="Widget", state="OPEN", url="https://github.com/owner/repo/issues/7", body="It breaks", tags=["bug"], parent=3)
    captured: dict = {}

    def fake_get_details(gh, owner, repo, number):
        captured.update(owner=owner, repo=repo, number=number)
        return details

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.get_details", fake_get_details)

    text = runner.invoke(app, ["issue", "show", "7"])
    as_json = runner.invoke(app, ["issue", "show", "7", "--json"])

    assert text.exit_code == 0, text.output
    assert captured == {"owner": "owner", "repo": "repo", "number": 7}
    assert "#7 [OPEN] Widget" in text.output
    assert "Parent: #3" in text.output
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.output)["parent"] == 3


def test_issue_show_ado(monkeypatch) -> None:
    from bmsdna.devtools.issue_show import IssueDetails

    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: AdoRemote("myorg", "MyProj", "myrepo"))
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    captured: dict = {}

    def fake_get_details(session, remote, number):
        captured.update(remote=remote, number=number)
        return IssueDetails(number=number, title="Task", state="Active", url="u", body="", tags=[], fields={"Iteration": "MyProj\\Sprint 1"})

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.get_details", fake_get_details)

    result = runner.invoke(app, ["issue", "show", "12"])

    assert result.exit_code == 0, result.output
    assert captured["number"] == 12
    assert "Iteration: MyProj\\Sprint 1" in result.output


def test_issue_create_ado_passes_iteration_parent_and_blockers(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: AdoRemote("myorg", "MyProj", "myrepo"))
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    captured: dict = {}

    def fake_create(session, remote, work_item_type, title, description, board, tags, screenshot_paths, file_paths, iteration=None, parent=None, blocked_by=None):
        captured.update(iteration=iteration, parent=parent, blocked_by=blocked_by)
        return {"id": 1}

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.create", fake_create)

    result = runner.invoke(
        app, ["issue", "create", "--title", "t", "--iteration", "MyProj\\Sprint 1", "--parent", "5", "--blocked-by", "7", "--blocked-by", "8"]
    )

    assert result.exit_code == 0, result.output
    assert captured == {"iteration": "MyProj\\Sprint 1", "parent": 5, "blocked_by": [7, 8]}


def test_issue_create_ado_iteration_defaults_to_pyproject(monkeypatch, tmp_path) -> None:
    _ado_remote_with_config(monkeypatch, tmp_path, "[tool.bdt.ado]\niteration = 'MyProj\\Sprint 9'\n")
    captured: dict = {}

    def fake_create(session, remote, work_item_type, title, description, board, tags, screenshot_paths, file_paths, iteration=None, parent=None, blocked_by=None):
        captured["iteration"] = iteration
        return {"id": 1}

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.create", fake_create)

    assert runner.invoke(app, ["issue", "create", "--title", "t"]).exit_code == 0
    assert captured["iteration"] == "MyProj\\Sprint 9"
    assert runner.invoke(app, ["issue", "create", "--title", "t", "--iteration", "MyProj\\Sprint 1"]).exit_code == 0
    assert captured["iteration"] == "MyProj\\Sprint 1"


def test_issue_update_ado_iteration_does_not_default_to_pyproject(monkeypatch, tmp_path) -> None:
    _ado_remote_with_config(monkeypatch, tmp_path, "[tool.bdt.ado]\niteration = 'MyProj\\Sprint 9'\n")
    captured: dict = {}

    def fake_update(session, remote, number, title, description, board, tags, remove_tags, state, iteration=None, parent=None, blocked_by=None):
        captured.update(iteration=iteration, parent=parent, blocked_by=blocked_by)

    monkeypatch.setattr("bmsdna.devtools.cli.ado_issue.update", fake_update)

    assert runner.invoke(app, ["issue", "update", "1", "--title", "t"]).exit_code == 0
    assert captured["iteration"] is None
    result = runner.invoke(app, ["issue", "update", "1", "--iteration", "MyProj\\Sprint 2", "--parent", "5", "--blocked-by", "7"])
    assert result.exit_code == 0, result.output
    assert captured == {"iteration": "MyProj\\Sprint 2", "parent": 5, "blocked_by": [7]}


def test_issue_create_github_passes_parent_and_blockers(monkeypatch) -> None:
    _github(monkeypatch)
    captured: dict = {}

    def fake_create(gh, owner, repo, title, description, labels, screenshot_paths, extra_args, file_paths=None, board=None, parent=None, blocked_by=None):
        captured.update(parent=parent, blocked_by=blocked_by)

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.create", fake_create)

    result = runner.invoke(app, ["issue", "create", "--title", "t", "--parent", "5", "--blocked-by", "7"])

    assert result.exit_code == 0, result.output
    assert captured == {"parent": 5, "blocked_by": [7]}


def test_issue_update_github_passes_parent_and_blockers(monkeypatch) -> None:
    _github(monkeypatch)
    captured: dict = {}

    def fake_update(gh, number, title, description, add_labels, remove_labels, state, parent=None, blocked_by=None):
        captured.update(parent=parent, blocked_by=blocked_by)

    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.update", fake_update)

    result = runner.invoke(app, ["issue", "update", "1", "--parent", "5", "--blocked-by", "7", "--blocked-by", "8"])

    assert result.exit_code == 0, result.output
    assert captured == {"parent": 5, "blocked_by": [7, 8]}


def test_issue_create_and_update_github_reject_iteration(monkeypatch) -> None:
    """GitHub issues have no iteration (Azure DevOps only): usage error (2), nothing created."""
    _github(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.create", lambda *a, **k: pytest.fail("must not create"))
    monkeypatch.setattr("bmsdna.devtools.cli.gh_issue.update", lambda *a, **k: pytest.fail("must not update"))

    assert runner.invoke(app, ["issue", "create", "--title", "t", "--iteration", "Sprint 1"]).exit_code == 2
    assert runner.invoke(app, ["issue", "update", "1", "--iteration", "Sprint 1"]).exit_code == 2
