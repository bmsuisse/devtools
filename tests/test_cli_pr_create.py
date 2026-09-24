"""`bdt pr create` must pass the parsed org/project/repo through to
`az repos pr create` explicitly, rather than relying on `az`'s own remote
auto-detection -- that auto-detect doesn't understand the SSH `v3` remote
form at all, and separately, an `az devops configure -d project=...` default
that happens to point at a *different* ADO project than the current repo's
own silently makes `az` look for the PR's repository in the wrong project.
"""

import json
from unittest.mock import MagicMock

from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote

runner = CliRunner()


def test_pr_create_passes_organization_project_repository_to_az(monkeypatch) -> None:
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0)

    monkeypatch.setattr("bmsdna.devtools.cli.subprocess.run", fake_run)
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)
    # No issue to link/tag here -- just needs to not make a real network call.
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda session, remote, source_branch, target: {"pullRequestId": 1, "description": ""})

    result = runner.invoke(app, ["pr", "create", "--target", "test"])

    assert result.exit_code == 0, result.output
    assert "--organization" in captured_cmd
    assert captured_cmd[captured_cmd.index("--organization") + 1] == "https://dev.azure.com/bmeurope"
    assert "--project" in captured_cmd
    assert captured_cmd[captured_cmd.index("--project") + 1] == "BMS - CCMT2"
    assert "--repository" in captured_cmd
    assert captured_cmd[captured_cmd.index("--repository") + 1] == "BMS - CCMT2"


def test_pr_create_passes_labels_and_prints_web_link_for_ado(monkeypatch) -> None:
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda session, remote, source_branch, target: {"pullRequestId": 456, "description": ""})

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps({"pullRequestId": 456, "title": "feat: widgets"}), stderr="")

    monkeypatch.setattr("bmsdna.devtools.cli.subprocess.run", fake_run)

    result = runner.invoke(app, ["pr", "create", "--target", "test", "--label", "bug", "--label", "urgent"])

    assert result.exit_code == 0, result.output
    assert "--labels" in captured_cmd
    labels_idx = captured_cmd.index("--labels")
    assert captured_cmd[labels_idx + 1 : labels_idx + 3] == ["bug", "urgent"]
    assert "https://dev.azure.com/bmeurope/BMS%20-%20CCMT2/_git/BMS%20-%20CCMT2/pullrequest/456" in result.output
    assert "PR #456: feat: widgets" in result.output
    assert '"pullRequestId"' not in result.output  # the raw `az` JSON blob must not be dumped on success


def test_pr_create_passes_labels_and_prints_web_link_for_github(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)
    # No issue to link/label -- keep this test about --label only.
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr_body", lambda gh: (7, ""))

    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--label", "bug"])

    assert result.exit_code == 0, result.output
    create_cmd = next(cmd for cmd in captured_cmds if "create" in cmd)
    assert create_cmd.count("--label") == 1
    assert create_cmd[create_cmd.index("--label") + 1] == "bug"
    assert "https://github.com/owner/repo/pull/7" in result.output


def test_pr_create_defaults_to_draft_and_prints_publish_hint_for_github(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr_body", lambda gh: (7, ""))

    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    result = runner.invoke(app, ["pr", "create", "--target", "main"])

    assert result.exit_code == 0, result.output
    create_cmd = next(cmd for cmd in captured_cmds if "create" in cmd)
    assert "--draft" in create_cmd
    assert "https://github.com/owner/repo/pull/7" in result.output
    assert "bdt pr publish" in result.output


def test_pr_create_no_draft_skips_draft_flag_and_publish_hint_for_github(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr="")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.subprocess.run", fake_run)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--no-draft"])

    assert result.exit_code == 0, result.output
    assert "--draft" not in captured_cmd
    assert "https://github.com/owner/repo/pull/7" in result.output
    assert "bdt pr publish" not in result.output


def test_pr_create_defaults_to_draft_for_ado(monkeypatch) -> None:
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda session, remote, source_branch, target: {"pullRequestId": 456, "description": ""})

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd[:] = cmd
        return MagicMock(returncode=0, stdout=json.dumps({"pullRequestId": 456, "title": "feat: widgets"}), stderr="")

    monkeypatch.setattr("bmsdna.devtools.cli.subprocess.run", fake_run)

    result = runner.invoke(app, ["pr", "create", "--target", "test"])

    assert result.exit_code == 0, result.output
    assert "--draft" in captured_cmd
    assert captured_cmd[captured_cmd.index("--draft") + 1] == "true"
    assert "bdt pr publish" in result.output


def test_pr_create_fails_before_touching_gh_az_when_required_label_group_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_labels.required_label_groups",
        lambda: {"risk": ["breaking", "non-breaking"]},
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not be reached — the missing-label check must run first")

    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", fail_if_called)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--label", "bug"])

    assert result.exit_code != 0
    assert "risk" in result.output
    assert "breaking" in result.output


def test_pr_create_rejects_missing_file(monkeypatch) -> None:
    result = runner.invoke(app, ["pr", "create", "--target", "main", "--file", "/nonexistent/report.pdf"])

    assert result.exit_code != 0
    assert "File not found" in result.output


def test_pr_create_attaches_files_for_github(monkeypatch, tmp_path) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.subprocess.run",
        lambda cmd, **kwargs: MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr=""),
    )

    captured: dict = {}

    def fake_add_attachments(gh, owner, repo, branch, screenshot_paths, file_paths):
        captured["file_paths"] = file_paths

    monkeypatch.setattr("bmsdna.devtools.gh_pr.add_attachments", fake_add_attachments)

    report = tmp_path / "report.pdf"
    report.write_bytes(b"fake-pdf-bytes")

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--file", str(report)])

    assert result.exit_code == 0, result.output
    assert captured["file_paths"] == [str(report)]


def test_pr_create_proceeds_when_required_label_group_satisfied(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr(
        "bmsdna.devtools.cli.pr_labels.required_label_groups",
        lambda: {"risk": ["breaking", "non-breaking"]},
    )
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.subprocess.run",
        lambda cmd, **kwargs: MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr=""),
    )

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--label", "non-breaking"])

    assert result.exit_code == 0, result.output


# -- --issue: linking / 'pr-available' labeling ------------------------------


def test_pr_create_rejects_issue_url_for_a_different_github_repo(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)

    def fail_if_called() -> str:
        raise AssertionError("should not be reached — --issue validation must run first")

    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", fail_if_called)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--issue", "https://github.com/other/repo/issues/9"])

    assert result.exit_code != 0
    assert "owner/repo" in result.output


def test_pr_create_github_issue_flag_links_and_labels(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.subprocess.run",
        lambda cmd, **kwargs: MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr=""),
    )
    # The PR body ("autofilled" from the commit message by --fill) doesn't mention #10 on its
    # own -- only the explicit --issue flag should bring it in.
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr_body", lambda gh: (7, "autofilled body"))

    linked: list[tuple[int, int]] = []
    labeled: list[int] = []
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.link_issue_to_pr",
        lambda gh, pr_number, body, number: (linked.append((pr_number, number)) or f"{body}\nFixes #{number}"),
    )
    monkeypatch.setattr("bmsdna.devtools.gh_issue.ensure_pr_available_label", lambda gh: None)
    monkeypatch.setattr("bmsdna.devtools.gh_issue.add_pr_available_label", lambda gh, number: labeled.append(number))

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--issue", "10"])

    assert result.exit_code == 0, result.output
    assert linked == [(7, 10)]
    assert labeled == [10]


def test_pr_create_github_body_scan_auto_links_without_issue_flag(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.subprocess.run",
        lambda cmd, **kwargs: MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr=""),
    )
    # The autofilled body already says "Fixes #5" (e.g. from the commit message) -- no --issue
    # flag needed for it to get linked/labeled.
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr_body", lambda gh: (7, "Fixes #5"))

    linked: list[tuple[int, int]] = []
    labeled: list[int] = []
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.link_issue_to_pr",
        lambda gh, pr_number, body, number: (linked.append((pr_number, number)) or body),
    )
    monkeypatch.setattr("bmsdna.devtools.gh_issue.ensure_pr_available_label", lambda gh: None)
    monkeypatch.setattr("bmsdna.devtools.gh_issue.add_pr_available_label", lambda gh, number: labeled.append(number))

    result = runner.invoke(app, ["pr", "create", "--target", "main"])

    assert result.exit_code == 0, result.output
    assert linked == [(7, 5)]
    assert labeled == [5]


def test_pr_create_ado_issue_flag_links_and_tags(monkeypatch) -> None:
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)
    monkeypatch.setattr(
        "bmsdna.devtools.cli.subprocess.run",
        lambda cmd, **kwargs: MagicMock(
            returncode=0, stdout=json.dumps({"pullRequestId": 456, "title": "feat: widgets", "description": "no mentions here"}), stderr=""
        ),
    )
    # Issue-linking re-fetches the PR fresh rather than trusting `az`'s stdout JSON.
    monkeypatch.setattr(
        "bmsdna.devtools.pr_build.get_pr", lambda session, remote, source_branch, target: {"pullRequestId": 456, "description": "no mentions here"}
    )

    linked: list[tuple[int, int]] = []
    tagged: list[int] = []
    monkeypatch.setattr(
        "bmsdna.devtools.pr_build.link_work_item", lambda session, remote, pr_id, work_item_id: linked.append((pr_id, work_item_id))
    )
    monkeypatch.setattr("bmsdna.devtools.ado_issue.add_pr_available_tag", lambda session, remote, work_item_id: tagged.append(work_item_id))

    result = runner.invoke(app, ["pr", "create", "--target", "test", "--issue", "20"])

    assert result.exit_code == 0, result.output
    assert linked == [(456, 20)]
    assert tagged == [20]


def test_pr_create_ado_body_scan_auto_links_work_item_url(monkeypatch) -> None:
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)
    description = "Relates to https://dev.azure.com/bmeurope/BMS%20-%20CCMT2/_workitems/edit/33"
    monkeypatch.setattr(
        "bmsdna.devtools.cli.subprocess.run",
        lambda cmd, **kwargs: MagicMock(
            returncode=0, stdout=json.dumps({"pullRequestId": 456, "title": "feat: widgets", "description": description}), stderr=""
        ),
    )
    monkeypatch.setattr(
        "bmsdna.devtools.pr_build.get_pr", lambda session, remote, source_branch, target: {"pullRequestId": 456, "description": description}
    )

    linked: list[tuple[int, int]] = []
    tagged: list[int] = []
    monkeypatch.setattr(
        "bmsdna.devtools.pr_build.link_work_item", lambda session, remote, pr_id, work_item_id: linked.append((pr_id, work_item_id))
    )
    monkeypatch.setattr("bmsdna.devtools.ado_issue.add_pr_available_tag", lambda session, remote, work_item_id: tagged.append(work_item_id))

    result = runner.invoke(app, ["pr", "create", "--target", "test"])

    assert result.exit_code == 0, result.output
    assert linked == [(456, 33)]
    assert tagged == [33]


def test_pr_create_github_issue_link_failure_is_a_warning_not_a_failed_create(monkeypatch) -> None:
    remote = GitHubRemote("owner", "repo")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.subprocess.run",
        lambda cmd, **kwargs: MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/7\n", stderr=""),
    )

    def raise_system_exit(gh):
        raise SystemExit("gh pr view failed")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr_body", raise_system_exit)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--issue", "10"])

    # The PR itself was created successfully -- a failure in the best-effort linking step must
    # not flip the overall exit code or hide the PR link.
    assert result.exit_code == 0, result.output
    assert "https://github.com/owner/repo/pull/7" in result.output
    assert "Warning: PR created, but linking issue(s)" in result.output


def test_pr_create_ado_issue_link_isolates_systemexit_from_one_bad_work_item(monkeypatch) -> None:
    """Regression: `ado_issue.add_pr_available_tag` can `sys.exit` (e.g. a 404 from
    `get_work_item_tags`), not just raise a `requests` error -- one bad `--issue` number must
    not stop the rest from being linked/tagged, and must not fail the overall `pr create`.
    """
    remote = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: remote)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_az", lambda: "az")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)
    monkeypatch.setattr(
        "bmsdna.devtools.cli.subprocess.run",
        lambda cmd, **kwargs: MagicMock(returncode=0, stdout=json.dumps({"pullRequestId": 456, "title": "feat: widgets"}), stderr=""),
    )
    monkeypatch.setattr("bmsdna.devtools.pr_build.get_pr", lambda session, remote, source_branch, target: {"pullRequestId": 456, "description": ""})
    monkeypatch.setattr("bmsdna.devtools.pr_build.link_work_item", lambda session, remote, pr_id, work_item_id: None)

    tagged: list[int] = []

    def fake_add_tag(session, remote, work_item_id):
        if work_item_id == 20:
            raise SystemExit(f"Work item #{work_item_id} not found in project 'BMS - CCMT2'.")
        tagged.append(work_item_id)

    monkeypatch.setattr("bmsdna.devtools.ado_issue.add_pr_available_tag", fake_add_tag)

    result = runner.invoke(app, ["pr", "create", "--target", "test", "--issue", "20", "--issue", "30"])

    assert result.exit_code == 0, result.output
    assert tagged == [30]  # the second --issue still got tagged despite the first raising
    assert "Warning: PR created, but linking work item(s)" in result.output
