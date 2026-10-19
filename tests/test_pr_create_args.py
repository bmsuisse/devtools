import io

import pytest

from bmsdna.devtools import pr_create_args as m
from bmsdna.devtools.pr_create_args import PrCreateArgsError, PrCreateOptions, parse_pr_create_args, resolve_body, unsupported_notes


def gh(*args: str) -> PrCreateOptions:
    return parse_pr_create_args(list(args), "github")


def ado(*args: str) -> PrCreateOptions:
    return parse_pr_create_args(list(args), "ado")


def test_title_and_multi_value_description_both_spellings() -> None:
    o = ado("--title", "T", "--description", "a", "b", "-d=c")
    assert o.title == "T"
    assert o.body == "c"  # last one wins
    assert ado("--description", "a", "b", "--squash").body == "a\nb"
    assert ado("--description=x=y").body == "x=y"
    assert gh("--body", "x", "-t", "Short").title == "Short"
    assert gh("-b=y").body == "y"


def test_host_dependent_short_flags() -> None:
    assert gh("-d").draft is True
    assert gh("-d", "false").draft is False
    assert ado("-d", "text").body == "text"
    assert gh("-r", "alice,bob").reviewers == ["alice", "bob"]
    assert ado("-r", "Repo").repository == "Repo"
    assert gh("--project", "Board").project == "Board"
    assert gh("-p", "Board").project == "Board"
    assert ado("--project", "Proj").ado_project == "Proj"
    assert ado("-p", "Proj").ado_project == "Proj"
    with pytest.raises(PrCreateArgsError):
        gh("--repository", "x")  # Azure DevOps only


def test_union_of_gh_and_az_spellings() -> None:
    o = gh("--base", "main", "--head", "feat")
    assert (o.base, o.head) == ("main", "feat")
    o = ado("--target-branch", "refs/heads/main", "-s", "refs/heads/feat")
    assert (o.base, o.head) == ("main", "feat")
    assert gh("-B", "dev", "-H", "x").base == "dev"
    assert gh("--label", "a", "-l", "b,c").labels == ["a", "b", "c"]
    assert ado("--labels", "a", "b").labels == ["a", "b"]
    assert gh("--reviewer", "a").reviewers == ["a"]
    assert ado("--reviewers", "a@x.com", "b@x.com", "--required-reviewers", "c@x.com").reviewers == ["a@x.com", "b@x.com"]
    assert ado("--required-reviewers", "c@x.com").required_reviewers == ["c@x.com"]
    assert gh("-a", "@me").assignees == ["@me"]
    assert gh("--milestone", "v1").milestone == "v1"
    assert gh("-m", "v1").milestone == "v1"
    assert ado("--work-items", "1", "2,3").work_items == ["1", "2", "3"]
    assert gh("--body-file", "f.md").body_file == "f.md"
    assert gh("-F", "-").body_file == "-"
    assert ado("--merge-commit-message", "msg", "x").merge_commit_message == "msg x"
    assert ado("--bypass-policy-reason", "why").bypass_policy_reason == "why"


def test_booleans_flag_value_and_equals_forms() -> None:
    o = ado("--squash", "--delete-source-branch", "false", "--auto-complete=true", "--bypass-policy", "--transition-work-items", "no")
    assert o.squash is True
    assert o.delete_source_branch is False
    assert o.auto_complete is True
    assert o.bypass_policy is True
    assert o.transition_work_items is False
    assert gh("--auto-merge").auto_complete is True
    assert gh("--fill", "--fill-first", "--fill-verbose", "--no-maintainer-edit", "--dry-run").dry_run is True
    assert gh("-w").web is True
    assert gh("-f").fill is True
    assert ado("--open").web is True
    assert ado("--draft", "true", "--squash").draft is True
    assert ado().draft is None
    with pytest.raises(PrCreateArgsError, match="true or false"):
        ado("--squash=maybe")


def test_ado_overrides_organization_url_is_reduced_to_name() -> None:
    assert ado("--org", "https://dev.azure.com/bmeurope/").organization == "bmeurope"
    assert ado("--organization=bmeurope").organization == "bmeurope"


def test_ignorable_flags_are_accepted_and_output_query_leave_a_note() -> None:
    o = ado("--detect", "true", "--verbose", "--debug", "--subscription", "sub", "--output", "json", "--query", "id")
    assert len(o.notes) == 2
    assert all("ignored" in n for n in o.notes)
    assert gh("-o", "json").notes


@pytest.mark.parametrize("args", [["--frobnicate"], ["stray"], ["--title"], ["--labels"], ["-x"]])
def test_unknown_flags_stray_values_and_missing_values_are_usage_errors(args) -> None:
    with pytest.raises(PrCreateArgsError) as e:
        ado(*args)
    assert isinstance(e.value, ValueError)
    if args[0] != "--title" and args[0] != "--labels":
        assert "--reviewers" in str(e.value)  # lists the supported flags


def test_resolve_body_from_file_stdin_and_conflict(tmp_path, monkeypatch) -> None:
    f = tmp_path / "b.md"
    f.write_text("from file", encoding="utf-8")
    assert resolve_body(gh("--body-file", str(f))) == "from file"
    monkeypatch.setattr(m.sys, "stdin", io.StringIO("from stdin"))
    assert resolve_body(gh("-F", "-")) == "from stdin"
    assert resolve_body(gh("--body", "x")) == "x"
    assert resolve_body(gh()) is None
    with pytest.raises(PrCreateArgsError, match="not both"):
        resolve_body(gh("--body", "x", "--body-file", str(f)))
    with pytest.raises(PrCreateArgsError, match="cannot read"):
        resolve_body(gh("--body-file", str(tmp_path / "missing")))


def test_unsupported_notes_per_host() -> None:
    notes = unsupported_notes(ado("--milestone", "v1", "-a", "bob", "--fill", "--no-maintainer-edit"), "ado")
    assert "--milestone is ignored on Azure DevOps" in notes
    assert "--assignee is ignored on Azure DevOps" in notes
    assert "--fill is ignored on Azure DevOps" in notes
    assert "--no-maintainer-edit is ignored on Azure DevOps" in notes
    assert unsupported_notes(ado("--reviewers", "a", "--squash"), "ado") == []
    notes = unsupported_notes(gh("--delete-source-branch", "--required-reviewers", "x", "--bypass-policy"), "github")
    assert "--delete-source-branch is ignored on GitHub" in notes
    assert "--required-reviewers is ignored on GitHub" in notes
    assert "--bypass-policy is ignored on GitHub" in notes
    assert unsupported_notes(gh("--delete-source-branch", "false"), "github") == []
    assert unsupported_notes(gh("--assignee", "@me", "--milestone", "m", "--project", "p"), "github") == []


def test_text_values_may_start_with_a_dash_unless_they_are_a_known_flag() -> None:
    assert gh("--body", "- first change\n- second").body == "- first change\n- second"
    assert ado("--description", "- bullet").body == "- bullet"
    assert gh("-b", "-1 is wrong", "--draft").draft is True
    assert gh("--title", "- odd title").title == "- odd title"
    assert gh("--merge-commit-message", "-x").merge_commit_message == "-x"
    assert gh("--body=- eq form").body == "- eq form"
    with pytest.raises(PrCreateArgsError, match="--body needs a value"):
        gh("--body", "--draft")  # a known flag where the value should be: a missing value
    with pytest.raises(PrCreateArgsError, match="--title needs a value"):
        gh("--title", "--label=x")


def test_list_values_still_stop_at_the_next_option_so_unknown_flags_are_reported() -> None:
    assert gh("--reviewer", "a", "b", "--draft").reviewers == ["a", "b"]
    with pytest.raises(PrCreateArgsError, match="unsupported flag"):
        gh("--reviewer", "a", "--bogus")


def test_extract_labels_is_host_independent_and_lenient() -> None:
    assert m.extract_labels(["--label", "bug", "-l", "a,b", "--labels=x", "--title", "T"]) == ["bug", "a", "b", "x"]
    assert m.extract_labels(["--bogus", "--label", "bug", "--also-unknown"]) == ["bug"]  # no validation of the rest
    assert m.extract_labels(["--title", "--label"]) == []
