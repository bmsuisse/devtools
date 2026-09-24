import pytest

from bmsdna.devtools.gitrepo import AdoRemote, GitHubRemote
from bmsdna.devtools.pr_issue_link import find_issue_refs_in_body, parse_issue_ref

GH_REMOTE = GitHubRemote(owner="owner", repo="repo")
ADO_REMOTE = AdoRemote(org="myorg", project="MyProj", repo="myrepo")


# -- parse_issue_ref --------------------------------------------------------


def test_parse_issue_ref_bare_number_github() -> None:
    assert parse_issue_ref("42", GH_REMOTE) == 42


def test_parse_issue_ref_bare_number_ado() -> None:
    assert parse_issue_ref(" 42 ", ADO_REMOTE) == 42


def test_parse_issue_ref_github_url_matching_repo() -> None:
    assert parse_issue_ref("https://github.com/owner/repo/issues/42", GH_REMOTE) == 42


def test_parse_issue_ref_github_url_case_insensitive() -> None:
    assert parse_issue_ref("https://github.com/Owner/Repo/issues/7", GH_REMOTE) == 7


def test_parse_issue_ref_github_url_wrong_repo_raises() -> None:
    with pytest.raises(ValueError, match="owner/repo"):
        parse_issue_ref("https://github.com/other/repo/issues/42", GH_REMOTE)


def test_parse_issue_ref_github_garbage_raises() -> None:
    with pytest.raises(ValueError):
        parse_issue_ref("not-a-number-or-url", GH_REMOTE)


def test_parse_issue_ref_ado_url_matching_org() -> None:
    assert parse_issue_ref("https://dev.azure.com/myorg/MyProj/_workitems/edit/99", ADO_REMOTE) == 99


def test_parse_issue_ref_ado_visualstudio_url_matching_org() -> None:
    assert parse_issue_ref("https://myorg.visualstudio.com/MyProj/_workitems/edit/99", ADO_REMOTE) == 99


def test_parse_issue_ref_ado_url_wrong_org_raises() -> None:
    with pytest.raises(ValueError, match="myorg"):
        parse_issue_ref("https://dev.azure.com/otherorg/MyProj/_workitems/edit/99", ADO_REMOTE)


def test_parse_issue_ref_ado_github_url_raises() -> None:
    with pytest.raises(ValueError):
        parse_issue_ref("https://github.com/owner/repo/issues/42", ADO_REMOTE)


# -- find_issue_refs_in_body -------------------------------------------------


def test_find_issue_refs_in_body_none_or_empty() -> None:
    assert find_issue_refs_in_body(None, GH_REMOTE) == []
    assert find_issue_refs_in_body("", GH_REMOTE) == []


@pytest.mark.parametrize(
    "keyword",
    ["Fixes", "fixes", "Fix", "Fixed", "Closes", "close", "Closed", "Resolves", "resolve", "Resolved"],
)
def test_find_issue_refs_in_body_github_keyword_variants(keyword: str) -> None:
    assert find_issue_refs_in_body(f"{keyword} #42", GH_REMOTE) == [42]


def test_find_issue_refs_in_body_github_multiple_keywords_deduped_and_ordered() -> None:
    body = "This fixes #3 and also closes #3 again, plus resolves #5."
    assert find_issue_refs_in_body(body, GH_REMOTE) == [3, 5]


def test_find_issue_refs_in_body_github_url_same_repo() -> None:
    body = "See https://github.com/owner/repo/issues/9 for context."
    assert find_issue_refs_in_body(body, GH_REMOTE) == [9]


def test_find_issue_refs_in_body_github_url_other_repo_ignored() -> None:
    body = "See https://github.com/someone/else/issues/9 for context."
    assert find_issue_refs_in_body(body, GH_REMOTE) == []


def test_find_issue_refs_in_body_github_plain_hash_number_not_matched() -> None:
    # A bare `#N` with no closing keyword isn't a GitHub "closes" reference.
    assert find_issue_refs_in_body("See #42 for background.", GH_REMOTE) == []


def test_find_issue_refs_in_body_ado_url() -> None:
    body = "Related work item: https://dev.azure.com/myorg/MyProj/_workitems/edit/17"
    assert find_issue_refs_in_body(body, ADO_REMOTE) == [17]


def test_find_issue_refs_in_body_ado_url_other_org_ignored() -> None:
    body = "https://dev.azure.com/otherorg/MyProj/_workitems/edit/17"
    assert find_issue_refs_in_body(body, ADO_REMOTE) == []


def test_find_issue_refs_in_body_ado_ignores_github_keywords() -> None:
    # Azure DevOps has no bare-#N keyword syntax of its own -- "Fixes #42" in an ADO PR
    # description isn't something this scan resolves to a work item.
    assert find_issue_refs_in_body("Fixes #42", ADO_REMOTE) == []
