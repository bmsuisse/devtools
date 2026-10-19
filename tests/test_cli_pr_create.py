"""`bdt pr create` wiring: remote detection, labels (explicit, scope-derived, required groups), draft default,
attachments and `--issue` linking, for GitHub (`gh_pr.create`) and Azure DevOps (`pr_build.create_pr`)."""

from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote

runner = CliRunner()
GH_URL = "https://github.com/owner/repo/pull/7"
ADO = AdoRemote("bmeurope", "BMS - CCMT2", "BMS - CCMT2")


def _github(monkeypatch, *, body: str = "") -> list[dict]:
    """Stub GitHub PR creation; returns the recorded `gh_pr.create` calls (kwargs, plus `target`/`args`)."""
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GitHubRemote("owner", "repo"))
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.require_gh", lambda: "gh")
    monkeypatch.setattr("bmsdna.devtools.gh_pr.has_build_policy", lambda gh, target: False)
    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr_body", lambda gh: (7, body))
    calls: list[dict] = []

    def fake_create(gh, target, extra_args, **kwargs):
        calls.append({"target": target, "args": extra_args, **kwargs})
        return 0, GH_URL

    monkeypatch.setattr("bmsdna.devtools.gh_pr.create", fake_create)
    return calls


def _ado(monkeypatch, *, description: str = "") -> list[dict]:
    """Stub Azure DevOps PR creation; returns the recorded `pr_build.create_pr` kwargs."""
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: ADO)
    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", lambda: "feature-x")
    monkeypatch.setattr("bmsdna.devtools.cli.auth_header", lambda pat: {})
    monkeypatch.setattr("bmsdna.devtools.pr_build.has_build_policy", lambda session, remote, target: False)
    monkeypatch.setattr(
        "bmsdna.devtools.pr_build.get_pr", lambda session, remote, source_branch, target: {"pullRequestId": 456, "description": description}
    )
    calls: list[dict] = []

    def fake_create(session, remote, source, target, **kwargs):
        calls.append({"remote": remote, "source": source, "target": target, **kwargs})
        return {"pullRequestId": 456, "title": kwargs.get("title") or "feat: widgets"}

    monkeypatch.setattr("bmsdna.devtools.pr_build.create_pr", fake_create)
    return calls


def test_pr_create_passes_remote_branches_to_ado(monkeypatch) -> None:
    calls = _ado(monkeypatch)

    result = runner.invoke(app, ["pr", "create", "--target", "test"])

    assert result.exit_code == 0, result.output
    assert calls[0]["remote"] == ADO
    assert (calls[0]["source"], calls[0]["target"]) == ("feature-x", "test")


def test_pr_create_passes_labels_and_prints_web_link_for_ado(monkeypatch) -> None:
    calls = _ado(monkeypatch)

    result = runner.invoke(app, ["pr", "create", "--target", "test", "--label", "bug", "--label", "urgent"])

    assert result.exit_code == 0, result.output
    assert calls[0]["labels"] == ["bug", "urgent"]
    assert "https://dev.azure.com/bmeurope/BMS%20-%20CCMT2/_git/BMS%20-%20CCMT2/pullrequest/456" in result.output
    assert "PR #456: feat: widgets" in result.output
    assert '"pullRequestId"' not in result.output


def test_pr_create_failure_on_ado_exits_nonzero_without_link(monkeypatch) -> None:
    _ado(monkeypatch)

    def failing_create(*args, **kwargs):
        raise SystemExit("Azure DevOps could not create the PR (HTTP 409): already exists")

    monkeypatch.setattr("bmsdna.devtools.pr_build.create_pr", failing_create)

    result = runner.invoke(app, ["pr", "create", "--target", "test"])

    assert result.exit_code == 1
    assert "already exists" in result.output
    assert "pullrequest/" not in result.output


def test_pr_create_passes_labels_and_prints_web_link_for_github(monkeypatch) -> None:
    calls = _github(monkeypatch)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--label", "bug"])

    assert result.exit_code == 0, result.output
    assert calls[0]["labels"] == ["bug"]
    assert calls[0]["target"] == "main"
    assert GH_URL in result.output


def test_pr_create_defaults_to_draft_and_prints_publish_hint_for_github(monkeypatch) -> None:
    calls = _github(monkeypatch)

    result = runner.invoke(app, ["pr", "create", "--target", "main"])

    assert result.exit_code == 0, result.output
    assert calls[0]["draft"] is True
    assert GH_URL in result.output
    assert "bdt pr publish" in result.output


def test_pr_create_no_draft_skips_draft_and_publish_hint_for_github(monkeypatch) -> None:
    calls = _github(monkeypatch)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--no-draft"])

    assert result.exit_code == 0, result.output
    assert calls[0]["draft"] is False
    assert "bdt pr publish" not in result.output


def test_pr_create_defaults_to_draft_for_ado(monkeypatch) -> None:
    calls = _ado(monkeypatch)

    result = runner.invoke(app, ["pr", "create", "--target", "test"])

    assert result.exit_code == 0, result.output
    assert calls[0]["draft"] is True
    assert "bdt pr publish" in result.output


def test_pr_create_fails_before_touching_the_host_when_required_label_group_missing(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.pr_labels.required_label_groups", lambda: {"risk": ["breaking", "non-breaking"]})

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
    _github(monkeypatch)
    captured: dict = {}

    def fake_add_attachments(gh, owner, repo, branch, screenshot_paths, file_paths):
        captured["file_paths"] = file_paths

    monkeypatch.setattr("bmsdna.devtools.gh_pr.add_attachments", fake_add_attachments)
    report = tmp_path / "report.pdf"
    report.write_bytes(b"fake-pdf-bytes")

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--file", str(report)])

    assert result.exit_code == 0, result.output
    assert captured["file_paths"] == [str(report)]


def test_pr_create_auto_applies_scope_label_for_github(monkeypatch) -> None:
    calls = _github(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.pr_labels.scope_labels", lambda: {"customers": "e2e-customers"})
    monkeypatch.setattr("bmsdna.devtools.cli.head_commit_subject", lambda: "feat(customers): add widget")

    result = runner.invoke(app, ["pr", "create", "--target", "main"])

    assert result.exit_code == 0, result.output
    assert calls[0]["labels"] == ["e2e-customers"]
    assert "Auto-applying label 'e2e-customers' for scope 'customers'" in result.output


def test_pr_create_auto_applies_scope_label_for_ado(monkeypatch) -> None:
    calls = _ado(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.pr_labels.scope_labels", lambda: {"customers": "e2e-customers"})
    monkeypatch.setattr("bmsdna.devtools.cli.head_commit_subject", lambda: "feat(customers): add widget")

    result = runner.invoke(app, ["pr", "create", "--target", "test"])

    assert result.exit_code == 0, result.output
    assert calls[0]["labels"] == ["e2e-customers"]


def test_pr_create_scope_label_dedups_against_explicit_label(monkeypatch) -> None:
    calls = _github(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.pr_labels.scope_labels", lambda: {"customers": "e2e-customers"})
    monkeypatch.setattr("bmsdna.devtools.cli.head_commit_subject", lambda: "feat(customers): add widget")

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--label", "e2e-customers"])

    assert result.exit_code == 0, result.output
    assert calls[0]["labels"] == ["e2e-customers"]  # not duplicated


def test_pr_create_no_scope_label_when_commit_has_no_scope(monkeypatch) -> None:
    calls = _github(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.pr_labels.scope_labels", lambda: {"customers": "e2e-customers"})
    monkeypatch.setattr("bmsdna.devtools.cli.head_commit_subject", lambda: "fix: no scope here")

    result = runner.invoke(app, ["pr", "create", "--target", "main"])

    assert result.exit_code == 0, result.output
    assert not calls[0]["labels"]


def test_pr_create_skips_commit_lookup_when_scope_labels_unconfigured(monkeypatch) -> None:
    """Opt-in: no [tool.bdt.pr.scope_labels] table means no git log call at all,
    not just no label -- so repos that don't configure it see zero behavior change."""
    _github(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.pr_labels.scope_labels", lambda: {})

    def fail_if_called():
        raise AssertionError("head_commit_subject should not be called when scope_labels is empty")

    monkeypatch.setattr("bmsdna.devtools.cli.head_commit_subject", fail_if_called)

    result = runner.invoke(app, ["pr", "create", "--target", "main"])

    assert result.exit_code == 0, result.output


def test_pr_create_proceeds_when_required_label_group_satisfied(monkeypatch) -> None:
    calls = _github(monkeypatch)
    monkeypatch.setattr("bmsdna.devtools.cli.pr_labels.required_label_groups", lambda: {"risk": ["breaking", "non-breaking"]})

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--label", "non-breaking"])

    assert result.exit_code == 0, result.output
    assert calls[0]["labels"] == ["non-breaking"]


def test_github_pr_create_passes_full_title_and_description(monkeypatch) -> None:
    calls = _github(monkeypatch)
    big = "line of description text\n" * 2000

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--title", "Long title " * 20, "--description", big])

    assert result.exit_code == 0, result.output
    assert calls[0]["title"] == "Long title " * 20
    assert calls[0]["description"] == big


# -- --issue: linking / 'pr-available' labeling ------------------------------


def test_pr_create_rejects_issue_url_for_a_different_github_repo(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.cli.current_remote", lambda: GitHubRemote("owner", "repo"))

    def fail_if_called() -> str:
        raise AssertionError("should not be reached — --issue validation must run first")

    monkeypatch.setattr("bmsdna.devtools.cli.current_branch", fail_if_called)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--issue", "https://github.com/other/repo/issues/9"])

    assert result.exit_code != 0
    assert "owner/repo" in result.output


def _record_github_links(monkeypatch) -> tuple[list[tuple[int, int]], list[int]]:
    linked: list[tuple[int, int]] = []
    labeled: list[int] = []
    monkeypatch.setattr(
        "bmsdna.devtools.gh_pr.link_issue_to_pr",
        lambda gh, pr_number, body, number: (linked.append((pr_number, number)) or f"{body}\nFixes #{number}"),
    )
    monkeypatch.setattr("bmsdna.devtools.gh_issue.ensure_pr_available_label", lambda gh: None)
    monkeypatch.setattr("bmsdna.devtools.gh_issue.add_pr_available_label", lambda gh, number: labeled.append(number))
    return linked, labeled


def test_pr_create_github_issue_flag_links_and_labels(monkeypatch) -> None:
    # The body doesn't mention #10 on its own -- only the explicit --issue flag should bring it in.
    _github(monkeypatch, body="autofilled body")
    linked, labeled = _record_github_links(monkeypatch)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--issue", "10"])

    assert result.exit_code == 0, result.output
    assert linked == [(7, 10)]
    assert labeled == [10]


def test_pr_create_github_body_scan_auto_links_without_issue_flag(monkeypatch) -> None:
    _github(monkeypatch, body="Fixes #5")
    linked, labeled = _record_github_links(monkeypatch)

    result = runner.invoke(app, ["pr", "create", "--target", "main"])

    assert result.exit_code == 0, result.output
    assert linked == [(7, 5)]
    assert labeled == [5]


def _record_ado_links(monkeypatch) -> tuple[list[tuple[int, int]], list[int]]:
    linked: list[tuple[int, int]] = []
    tagged: list[int] = []
    monkeypatch.setattr(
        "bmsdna.devtools.pr_build.link_work_item", lambda session, remote, pr_id, work_item_id: linked.append((pr_id, work_item_id))
    )
    monkeypatch.setattr("bmsdna.devtools.ado_issue.add_pr_available_tag", lambda session, remote, work_item_id: tagged.append(work_item_id))
    return linked, tagged


def test_pr_create_ado_issue_flag_links_and_tags(monkeypatch) -> None:
    _ado(monkeypatch, description="no mentions here")
    linked, tagged = _record_ado_links(monkeypatch)

    result = runner.invoke(app, ["pr", "create", "--target", "test", "--issue", "20"])

    assert result.exit_code == 0, result.output
    assert linked == [(456, 20)]
    assert tagged == [20]


def test_pr_create_ado_body_scan_auto_links_work_item_url(monkeypatch) -> None:
    _ado(monkeypatch, description="Relates to https://dev.azure.com/bmeurope/BMS%20-%20CCMT2/_workitems/edit/33")
    linked, tagged = _record_ado_links(monkeypatch)

    result = runner.invoke(app, ["pr", "create", "--target", "test"])

    assert result.exit_code == 0, result.output
    assert linked == [(456, 33)]
    assert tagged == [33]


def test_pr_create_github_issue_link_failure_is_a_warning_not_a_failed_create(monkeypatch) -> None:
    _github(monkeypatch)

    def raise_system_exit(gh):
        raise SystemExit("GitHub GET pulls failed")

    monkeypatch.setattr("bmsdna.devtools.gh_pr.get_pr_body", raise_system_exit)

    result = runner.invoke(app, ["pr", "create", "--target", "main", "--issue", "10"])

    # The PR itself was created successfully -- a failure in the best-effort linking step must
    # not flip the overall exit code or hide the PR link.
    assert result.exit_code == 0, result.output
    assert GH_URL in result.output
    assert "Warning: PR created, but linking issue(s)" in result.output


def test_pr_create_ado_issue_link_isolates_systemexit_from_one_bad_work_item(monkeypatch) -> None:
    """Regression: `ado_issue.add_pr_available_tag` can `sys.exit` (e.g. a 404 from
    `get_work_item_tags`), not just raise a `requests` error -- one bad `--issue` number must
    not stop the rest from being linked/tagged, and must not fail the overall `pr create`.
    """
    _ado(monkeypatch)
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
