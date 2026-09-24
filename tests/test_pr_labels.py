import pytest

from bmsdna.devtools.pr_labels import (
    format_missing_groups_error,
    label_for_scope,
    missing_label_groups,
    parse_conventional_scope,
    required_label_groups,
    scope_labels,
)


def write_pyproject(tmp_path, body: str):
    (tmp_path / "pyproject.toml").write_text(body)
    return tmp_path


def test_required_label_groups_empty_when_no_pyproject(tmp_path) -> None:
    assert required_label_groups(tmp_path) == {}


def test_required_label_groups_empty_when_table_absent(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.bdt.ado]\nboard = 'x'\n")
    assert required_label_groups(tmp_path) == {}


def test_required_label_groups_reads_named_groups(tmp_path) -> None:
    write_pyproject(
        tmp_path,
        "[tool.bdt.pr.required_labels]\n"
        'type = ["bug", "feature", "chore"]\n'
        'risk = ["breaking", "non-breaking"]\n',
    )
    assert required_label_groups(tmp_path) == {
        "type": ["bug", "feature", "chore"],
        "risk": ["breaking", "non-breaking"],
    }


def test_missing_label_groups_none_missing_when_all_satisfied() -> None:
    groups = {"type": ["bug", "feature"], "risk": ["breaking", "non-breaking"]}
    assert missing_label_groups(groups, ["feature", "breaking"]) == {}


def test_missing_label_groups_reports_unsatisfied_group() -> None:
    groups = {"type": ["bug", "feature"], "risk": ["breaking", "non-breaking"]}
    assert missing_label_groups(groups, ["feature"]) == {"risk": ["breaking", "non-breaking"]}


def test_missing_label_groups_matches_case_insensitively() -> None:
    groups = {"risk": ["breaking", "non-breaking"]}
    assert missing_label_groups(groups, ["Breaking"]) == {}
    assert missing_label_groups(groups, ["BREAKING"]) == {}


def test_missing_label_groups_reports_all_when_no_labels_given() -> None:
    groups = {"type": ["bug", "feature"], "risk": ["breaking", "non-breaking"]}
    assert missing_label_groups(groups, []) == groups


def test_format_missing_groups_error_lists_group_name_and_choices() -> None:
    msg = format_missing_groups_error({"risk": ["breaking", "non-breaking"]})
    assert "risk" in msg
    assert "breaking" in msg
    assert "non-breaking" in msg


def test_scope_labels_empty_when_no_pyproject(tmp_path) -> None:
    assert scope_labels(tmp_path) == {}


def test_scope_labels_empty_when_table_absent(tmp_path) -> None:
    write_pyproject(tmp_path, "[tool.bdt.pr.required_labels]\ntype = ['bug']\n")
    assert scope_labels(tmp_path) == {}


def test_scope_labels_reads_configured_mapping(tmp_path) -> None:
    write_pyproject(
        tmp_path,
        "[tool.bdt.pr.scope_labels]\n"
        'customers = "e2e-customers"\n'
        'billing = "e2e-billing"\n',
    )
    assert scope_labels(tmp_path) == {"customers": "e2e-customers", "billing": "e2e-billing"}


def test_scope_labels_ignores_non_string_values(tmp_path) -> None:
    write_pyproject(
        tmp_path,
        "[tool.bdt.pr.scope_labels]\n"
        'customers = "e2e-customers"\n'
        "billing = [\"not\", \"a\", \"string\"]\n",
    )
    assert scope_labels(tmp_path) == {"customers": "e2e-customers"}


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("feat(customers): add widget support", "customers"),
        ("fix(billing)!: correct rounding", "billing"),
        ("feat(some scope): spaces in scope", "some scope"),
        ("fix: no scope here", None),
        ("not a conventional commit at all", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_conventional_scope(subject: str | None, expected: str | None) -> None:
    assert parse_conventional_scope(subject) == expected


def test_label_for_scope_returns_none_when_scope_is_none() -> None:
    assert label_for_scope({"customers": "e2e-customers"}, None) is None


def test_label_for_scope_returns_none_when_scope_unconfigured() -> None:
    assert label_for_scope({"customers": "e2e-customers"}, "billing") is None


def test_label_for_scope_returns_configured_label() -> None:
    assert label_for_scope({"customers": "e2e-customers"}, "customers") == "e2e-customers"


def test_label_for_scope_matches_case_insensitively() -> None:
    assert label_for_scope({"Customers": "e2e-customers"}, "CUSTOMERS") == "e2e-customers"
