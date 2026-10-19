"""`pr_build.create_pr` with the `az repos pr create` flags: reviewers (identity lookup), work items, completion
options, auto-complete, dry-run body."""

from unittest.mock import MagicMock

import pytest
import requests

from bmsdna.devtools import pr_build
from bmsdna.devtools.gitrepo import AdoRemote
from bmsdna.devtools.pr_create_args import PrCreateOptions

REMOTE = AdoRemote("org", "Proj", "Repo")
GUID = "11111111-2222-3333-4444-555555555555"


def _resp(body=None, status=200) -> MagicMock:
    r = MagicMock(status_code=status, reason="x", text="")
    r.json.return_value = body if body is not None else {}
    return r


def _session(identities: dict[str, list[dict]] | None = None, created=None) -> MagicMock:
    session = MagicMock()

    def get(url, **kwargs):
        if "vssps" in url:
            return _resp({"value": (identities or {}).get(kwargs["params"]["filterValue"], [])})
        if "connectionData" in url:
            return _resp({"authenticatedUser": {"id": "me-id"}})
        raise AssertionError(url)

    session.get.side_effect = get
    session.post.return_value = _resp(created or {"pullRequestId": 7, "title": "t"})
    session.patch.return_value = _resp({})
    return session


def _create(session, **kw):
    return pr_build.create_pr(session, REMOTE, "feat", "dev", title="t", **kw)


def test_reviewers_resolved_by_mail_guid_and_marked_required() -> None:
    session = _session({"a@x.com": [{"id": "id-a", "providerDisplayName": "A"}], "g": [{"id": "id-g"}]})
    _create(session, options=PrCreateOptions(reviewers=["a@x.com", GUID], required_reviewers=["g", "a@x.com"]))
    body = session.post.call_args.kwargs["json"]
    assert body["reviewers"] == [{"id": "id-a", "isRequired": True}, {"id": GUID}, {"id": "id-g", "isRequired": True}]
    lookup = next(c for c in session.get.call_args_list if "vssps" in c.args[0])
    assert lookup.args[0].startswith("https://vssps.dev.azure.com/org/_apis/identities")
    assert lookup.kwargs["params"]["queryMembership"] == "None"
    assert not any(GUID in str(c) for c in session.get.call_args_list)  # a GUID is used directly


def test_reviewer_no_match_and_ambiguous_exit_clearly() -> None:
    session = _session({"nobody": [], "bob": [{"id": "1", "providerDisplayName": "Bob A"}, {"id": "2", "providerDisplayName": "Bob B"}]})
    with pytest.raises(SystemExit, match="No Azure DevOps user or group matches 'nobody'"):
        _create(session, options=PrCreateOptions(reviewers=["nobody"]))
    with pytest.raises(SystemExit, match="ambiguous"):
        _create(session, options=PrCreateOptions(reviewers=["bob"]))
    session.post.assert_not_called()  # no PR is created with a half-resolved reviewer list


def test_ambiguous_reviewer_resolved_by_exact_display_name_or_mail() -> None:
    people = [
        {"id": "1", "providerDisplayName": "Bob", "properties": {"Mail": {"$value": "bob@x.com"}}},
        {"id": "2", "providerDisplayName": "Bobby", "properties": {"Mail": {"$value": "bobby@x.com"}}},
    ]
    session = _session({"bob": people, "bobby@x.com": people})
    assert pr_build.resolve_identity(session, REMOTE, "bob") == "1"
    assert pr_build.resolve_identity(session, REMOTE, "bobby@x.com") == "2"


def test_identity_lookup_failure_exits() -> None:
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("down")
    with pytest.raises(SystemExit, match="Could not look up reviewer"):
        pr_build.resolve_identity(session, REMOTE, "a@x.com")


def test_work_items_are_deduplicated_across_flag_and_issue_refs() -> None:
    session = _session()
    _create(session, options=PrCreateOptions(work_items=["12", "#13"]), work_item_ids=[13, 14])
    assert session.post.call_args.kwargs["json"]["workItemRefs"] == [{"id": "12"}, {"id": "13"}, {"id": "14"}]


def test_work_items_must_be_numeric() -> None:
    with pytest.raises(ValueError, match="work item IDs"):
        pr_build.parse_work_item_ids(["abc"])


def test_no_extra_fields_and_no_follow_up_by_default() -> None:
    session = _session()
    _create(session)
    body = session.post.call_args.kwargs["json"]
    assert set(body) == {"sourceRefName", "targetRefName", "title", "isDraft"}
    session.patch.assert_not_called()


def test_completion_options_and_auto_complete_are_patched_after_creation() -> None:
    session = _session()
    opts = PrCreateOptions(
        auto_complete=True, squash=True, delete_source_branch=False, bypass_policy=True,
        bypass_policy_reason="hotfix", transition_work_items=True, merge_commit_message="msg",
    )
    pr = _create(session, options=opts)
    assert session.patch.call_args.args[0].endswith("/pullRequests/7")
    assert session.patch.call_args.kwargs["json"] == {
        "completionOptions": {
            "squashMerge": True, "deleteSourceBranch": False, "bypassPolicy": True, "transitionWorkItems": True,
            "mergeCommitMessage": "msg", "bypassReason": "hotfix",
        },
        "autoCompleteSetBy": {"id": "me-id"},
    }
    assert pr["pullRequestId"] == 7
    assert "autoCompleteSetBy" not in session.post.call_args.kwargs["json"]


def test_completion_flags_without_auto_complete_do_not_set_the_user() -> None:
    session = _session()
    _create(session, options=PrCreateOptions(squash=True))
    assert session.patch.call_args.kwargs["json"] == {"completionOptions": {"squashMerge": True}}
    session.get.assert_not_called()


def test_auto_complete_false_sets_nothing() -> None:
    session = _session()
    _create(session, options=PrCreateOptions(auto_complete=False))
    session.patch.assert_not_called()


def test_failed_completion_update_is_a_warning_not_a_failed_create(capsys) -> None:
    session = _session()
    session.patch.return_value = _resp({"message": "nope"}, status=403)
    session.patch.return_value.raise_for_status.side_effect = requests.HTTPError("403")
    pr = _create(session, options=PrCreateOptions(squash=True))
    assert pr["pullRequestId"] == 7
    assert "Warning: PR created, but setting auto-complete" in capsys.readouterr().err


def test_current_user_lookup_failure_exits_with_hint() -> None:
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("down")
    with pytest.raises(SystemExit, match="--auto-complete"):
        pr_build.current_user_id(session, REMOTE)


def test_build_create_body_makes_no_post() -> None:
    session = _session({"a@x.com": [{"id": "id-a"}]})
    body = pr_build.build_create_body(session, REMOTE, "feat", "dev", title="t", draft=True, options=PrCreateOptions(reviewers=["a@x.com"]))
    assert body["reviewers"] == [{"id": "id-a"}] and body["isDraft"] is True
    session.post.assert_not_called()
