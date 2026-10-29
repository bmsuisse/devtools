import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from bmsdna.devtools.ado_issue import get_details, search
from bmsdna.devtools.business_user import (
    BusinessUserDetector,
    CommitAuthors,
    Verdict,
    classify,
    get_license,
    read_commit_authors,
)
from bmsdna.devtools.gitrepo import AdoRemote
from bmsdna.devtools.issue_show import IssueDetails, format_details

REMOTE = AdoRemote("myorg", "MyProj", "myrepo")
DEV = {"id": "dev-id", "uniqueName": "dev@example.com", "displayName": "Dora Dev"}
SALES = {"id": "sales-id", "uniqueName": "sales@example.com", "displayName": "Sam Sales"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo_with_commits(tmp_path: Path, authors: list[tuple[str, str]]) -> Path:
    _git(tmp_path, "init", "-q")
    for i, (name, email) in enumerate(authors):
        _git(tmp_path, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "--allow-empty", "-m", f"c{i}")
    return tmp_path


def _entitlements(licenses: dict[str, str | int]) -> MagicMock:
    """A session whose entitlement API answers `licenses[id]` (an int is an HTTP error status)."""
    session = MagicMock()

    def get(url: str, **kwargs):
        identity_id = url.rsplit("/", 1)[1]
        outcome = licenses.get(identity_id, 404)
        response = MagicMock()
        response.status_code = outcome if isinstance(outcome, int) else 200
        response.json.return_value = {"accessLevel": {"accountLicenseType": outcome}} if isinstance(outcome, str) else {}
        return response

    session.get.side_effect = get
    return session


# -- read_commit_authors ------------------------------------------------------


def test_read_commit_authors_collects_authors_and_committers(tmp_path: Path) -> None:
    repo = _repo_with_commits(tmp_path, [("Dora Dev", "Dev@Example.com"), ("Other", "other@example.com")])

    authors = read_commit_authors(depth=10, cwd=repo)

    assert authors is not None
    assert authors.contains("dev@example.com", None)  # emails compare case-insensitively
    assert authors.contains(None, "OTHER")
    assert not authors.contains("sales@example.com", "Sam Sales")


def test_read_commit_authors_only_looks_at_the_last_depth_commits(tmp_path: Path) -> None:
    repo = _repo_with_commits(tmp_path, [("Old", "old@example.com"), ("New", "new@example.com")])

    authors = read_commit_authors(depth=1, cwd=repo)

    assert authors is not None
    assert authors.contains("new@example.com", None)
    assert not authors.contains("old@example.com", None)


def test_read_commit_authors_is_none_outside_a_git_repo(tmp_path: Path) -> None:
    assert read_commit_authors(cwd=tmp_path) is None


def test_read_commit_authors_is_none_for_a_shallow_clone_with_too_little_history(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    source = _repo_with_commits(tmp_path / "source", [("A", "a@example.com"), ("B", "b@example.com")])
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{source}", str(clone)], check=True, capture_output=True)

    assert read_commit_authors(depth=1000, cwd=clone) is None
    assert read_commit_authors(depth=1, cwd=clone) is not None  # the shallow history covers what was asked for


# -- get_license ---------------------------------------------------------------


def test_get_license_lowercases_the_account_license_type() -> None:
    assert get_license(_entitlements({"sales-id": "Stakeholder"}), REMOTE, "sales-id") == "stakeholder"


def test_get_license_is_none_when_the_api_refuses() -> None:
    assert get_license(_entitlements({"sales-id": 403}), REMOTE, "sales-id") is None
    assert get_license(_entitlements({}), REMOTE, "sales-id") is None


def test_get_license_calls_the_entitlement_api_of_the_org() -> None:
    session = _entitlements({"sales-id": "express"})

    get_license(session, REMOTE, "sales-id")

    assert session.get.call_args.args[0] == "https://vsaex.dev.azure.com/myorg/_apis/userentitlements/sales-id"


# -- classify -------------------------------------------------------------------

AUTHORS = CommitAuthors(emails=frozenset({"dev@example.com"}), names=frozenset({"dora dev"}), depth=1000)


def test_classify_stakeholder_is_a_business_user_whatever_the_commits() -> None:
    assert classify("stakeholder", AUTHORS, "dev@example.com", None) == Verdict(True, "Stakeholder license")


def test_classify_licensed_user_with_commits_is_not_a_business_user() -> None:
    assert classify("basic", AUTHORS, "dev@example.com", None).business_user is False
    assert classify(None, AUTHORS, None, "Dora Dev").business_user is False  # matched by name when the email differs


def test_classify_licensed_user_without_commits_is_a_business_user() -> None:
    verdict = classify("basic", AUTHORS, "sales@example.com", "Sam Sales")

    assert verdict.business_user is True
    assert "no commits in the last 1000" in verdict.reason


def test_classify_is_unknown_without_a_license_stakeholder_answer_and_without_history() -> None:
    assert classify("basic", None, "sales@example.com", None).business_user is None
    assert classify(None, None, "sales@example.com", None).business_user is None


# -- BusinessUserDetector ----------------------------------------------------------


def test_detector_falls_back_to_commits_when_the_entitlement_api_refuses(tmp_path: Path) -> None:
    repo = _repo_with_commits(tmp_path, [("Dora Dev", "dev@example.com")])
    detector = BusinessUserDetector(_entitlements({"dev-id": 403, "sales-id": 403}), REMOTE, repo_dir=repo)

    assert detector.verdict(DEV).business_user is False
    assert detector.verdict(SALES).business_user is True


def test_detector_stakeholder_needs_no_git_history(tmp_path: Path) -> None:
    detector = BusinessUserDetector(_entitlements({"sales-id": "stakeholder"}), REMOTE, repo_dir=tmp_path)  # not a git repo

    assert detector.verdict(SALES) == Verdict(True, "Stakeholder license")


def test_detector_asks_each_license_and_reads_the_history_once(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_commits(tmp_path, [("Dora Dev", "dev@example.com")])
    session = _entitlements({"dev-id": "advanced"})
    reads: list[int] = []
    monkeypatch.setattr("bmsdna.devtools.business_user.read_commit_authors", lambda depth, cwd: reads.append(depth) or AUTHORS)
    detector = BusinessUserDetector(session, REMOTE, repo_dir=repo)

    for _ in range(3):
        detector.verdict(DEV)

    assert session.get.call_count == 1
    assert reads == [1000]


def test_detector_without_a_creator_is_unknown() -> None:
    assert BusinessUserDetector(MagicMock(), REMOTE).verdict(None).business_user is None


# -- search / show integration -------------------------------------------------------


def test_search_business_users_keeps_only_items_created_by_business_users(tmp_path: Path, capsys) -> None:
    repo = _repo_with_commits(tmp_path, [("Dora Dev", "dev@example.com")])
    session = _entitlements({"dev-id": "advanced", "sales-id": "stakeholder"})
    session.post.return_value.json.return_value = {"workItems": [{"id": 1}, {"id": 2}]}
    work_items = {
        "value": [
            {"id": 1, "fields": {"System.Title": "Dev's", "System.State": "New", "System.CreatedBy": DEV}},
            {"id": 2, "fields": {"System.Title": "Sales'", "System.State": "New", "System.CreatedBy": SALES}},
        ]
    }
    default_get = session.get.side_effect

    def get(url: str, **kwargs):
        if url.endswith("/_apis/wit/workitems"):
            assert "System.CreatedBy" in kwargs["params"]["fields"]
            response = MagicMock()
            response.json.return_value = work_items
            return response
        return default_get(url, **kwargs)

    session.get.side_effect = get

    items = search(session, REMOTE, [], business_users=BusinessUserDetector(session, REMOTE, repo_dir=repo))

    assert [i["id"] for i in items] == [2]
    assert "Sales'" in capsys.readouterr().out


def test_search_without_business_users_does_not_ask_for_the_creator() -> None:
    session = MagicMock()
    session.post.return_value.json.return_value = {"workItems": [{"id": 1}]}
    session.get.return_value.json.return_value = {"value": [{"id": 1, "fields": {"System.Title": "t", "System.State": "New"}}]}

    search(session, REMOTE, [])

    assert "System.CreatedBy" not in session.get.call_args.kwargs["params"]["fields"]


def test_get_details_names_the_creator_and_whether_they_are_a_business_user(monkeypatch) -> None:
    session = MagicMock()
    work_item = {"fields": {"System.Title": "t", "System.State": "New", "System.CreatedBy": SALES}}
    monkeypatch.setattr("bmsdna.devtools.ado_issue.get_work_item", lambda *a: work_item)
    monkeypatch.setattr("bmsdna.devtools.ado_issue.list_all_comments", lambda *a: [])
    detector = BusinessUserDetector(_entitlements({"sales-id": "stakeholder"}), REMOTE)

    details = get_details(session, REMOTE, 5, detector)

    assert (details.created_by, details.business_user, details.business_user_reason) == ("Sam Sales", True, "Stakeholder license")
    assert "Created by: Sam Sales (business user: Stakeholder license)" in format_details(details).splitlines()


def test_get_details_without_a_detector_names_the_creator_but_judges_nothing(monkeypatch) -> None:
    work_item = {"fields": {"System.Title": "t", "System.State": "New", "System.CreatedBy": SALES}}
    monkeypatch.setattr("bmsdna.devtools.ado_issue.get_work_item", lambda *a: work_item)
    monkeypatch.setattr("bmsdna.devtools.ado_issue.list_all_comments", lambda *a: [])

    details = get_details(MagicMock(), REMOTE, 5)

    assert details.business_user is None
    assert "Created by: Sam Sales" in format_details(details).splitlines()


def test_format_details_shows_a_developer_creator_and_an_unknown_one() -> None:
    def details(business_user: bool | None, reason: str) -> IssueDetails:
        return IssueDetails(
            number=1, title="t", state="New", url="u", body="", tags=[], created_by="Dora", business_user=business_user, business_user_reason=reason
        )

    developer = details(False, "advanced license, has commits")
    unknown = details(None, "license unknown, commit history unavailable")

    assert "Created by: Dora (developer: advanced license, has commits)" in format_details(developer)
    assert "Created by: Dora (unknown: license unknown, commit history unavailable)" in format_details(unknown)
