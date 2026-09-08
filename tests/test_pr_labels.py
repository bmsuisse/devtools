from bmsdna.devtools.pr_labels import format_missing_groups_error, missing_label_groups, required_label_groups


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


def test_missing_label_groups_reports_all_when_no_labels_given() -> None:
    groups = {"type": ["bug", "feature"], "risk": ["breaking", "non-breaking"]}
    assert missing_label_groups(groups, []) == groups


def test_format_missing_groups_error_lists_group_name_and_choices() -> None:
    msg = format_missing_groups_error({"risk": ["breaking", "non-breaking"]})
    assert "risk" in msg
    assert "breaking" in msg
    assert "non-breaking" in msg
