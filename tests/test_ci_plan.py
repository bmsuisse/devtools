import pytest

from bmsdna.devtools import ci_plan
from bmsdna.devtools.ci_plan import (
    BranchFlowError,
    Context,
    Plan,
    PlanConfig,
    Tier,
    load_plan_config,
    make_plan,
)


def touch(root, *paths: str) -> None:
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("")


@pytest.fixture
def repo(tmp_path):
    """Folder-mode unit tier (orders, customers, shared) and a glob-mode flat e2e tier."""
    touch(
        tmp_path,
        "tests/unit/orders/test_a.py",
        "tests/unit/customers/test_b.py",
        "tests/unit/shared/test_c.py",
    )
    touch(tmp_path, "tests/unit/__pycache__/x.pyc", "tests/unit/conftest.py")
    touch(
        tmp_path,
        "tests/e2e/test_chat_one.py",
        "tests/e2e/test_chat_two.py",
        "tests/e2e/test_review_x.py",
        "tests/e2e/test_misc.py",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.bdt.ci.plan]\nnon_code = ["docs/**", "*.md"]\n'
        '[tool.bdt.ci.plan.tiers.unit]\ndir = "tests/unit"\n'
        '[tool.bdt.ci.plan.tiers.e2e]\ndir = "tests/e2e"\n'
        '[tool.bdt.ci.plan.tiers.e2e.areas]\nchat = ["test_chat*.py"]\nreview = ["test_review_*.py"]\n'
    )
    return tmp_path


@pytest.fixture
def config(repo) -> PlanConfig:
    loaded = load_plan_config(repo)
    assert loaded is not None
    return loaded


def no_fix_check() -> None:
    return None


def plan_for(
    config, ctx, *, labels=(), files=(), fix_base=no_fix_check, on_missing=None
) -> Plan:
    if on_missing:
        config = PlanConfig(config.root, config.tiers, on_missing, config.non_code)

    def read_labels() -> list[str]:
        if isinstance(labels, Exception):
            raise labels
        return list(labels)

    def read_files() -> list[str]:
        if isinstance(files, Exception):
            raise files
        return list(files)

    return make_plan(
        ctx,
        config,
        labels=read_labels,
        changed_files=read_files,
        verify_fix_base=fix_base,
    )


def pr(source="feat/x", target="dev", **kw) -> Context:
    return Context(pr_id=7, source=source, target=target, **kw)


# --- config and areas -------------------------------------------------------------------------------------------


def test_config_absent_without_table(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.bdt.ado]\nboard = 'x'\n")
    assert load_plan_config(tmp_path) is None


def test_empty_table_uses_default_tiers(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.bdt.ci.plan]\n")
    config = load_plan_config(tmp_path)
    assert config is not None
    assert [(t.name, t.dir) for t in config.tiers] == [
        ("unit", "tests/unit"),
        ("e2e", "tests/e2e"),
    ]
    assert config.on_missing == "infer"


@pytest.mark.parametrize(
    "body",
    [
        'on_missing = "maybe"',
        "non_code = 'docs'",
        "tiers = 3",
        "[tool.bdt.ci.plan.tiers.unit]\ndir = 4",
    ],
)
def test_invalid_config_is_rejected(tmp_path, body) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.bdt.ci.plan]\n{body}\n"
        if "[" not in body
        else f"{body}\n[tool.bdt.ci.plan]\n"
    )
    with pytest.raises(ValueError, match=r"tool\.bdt\.ci\.plan"):
        load_plan_config(tmp_path)


def test_folder_areas_skip_private_dirs(config) -> None:
    assert config.tiers[0].areas(config.root) == ["customers", "orders", "shared"]


def test_glob_areas_add_implicit_shared_for_unmatched_files(config) -> None:
    assert config.tiers[1].areas(config.root) == ["chat", "review", "shared"]


def test_area_label_is_kebab_case() -> None:
    assert ci_plan.area_label("route_planning") == "test-route-planning"


def test_required_label_choices_include_full_none_and_legacy(config) -> None:
    choices = ci_plan.required_label_choices(config)
    assert {"test-orders", "test-full", "test-none", "e2e-orders", "e2e-none"} <= set(
        choices
    )


def test_paths_folder_and_glob_mode(config) -> None:
    unit, e2e = config.tiers
    assert unit.paths(config.root, {"orders"}) == ["tests/unit/orders"]
    assert unit.paths(config.root, None) == ["tests/unit"]
    assert e2e.paths(config.root, {"chat"}) == [
        "tests/e2e/test_chat_one.py",
        "tests/e2e/test_chat_two.py",
    ]
    assert e2e.paths(config.root, {"shared"}) == ["tests/e2e/test_misc.py"]
    assert len(e2e.paths(config.root, None)) == 4


def test_missing_tier_dir_has_no_areas_or_paths(tmp_path) -> None:
    tier = Tier("unit", "tests/unit")
    assert tier.areas(tmp_path) == [] and tier.paths(tmp_path, None) == []


def test_area_of(config) -> None:
    unit, e2e = config.tiers
    assert unit.area_of("tests/unit/orders/test_a.py") == {"orders"}
    assert unit.area_of("tests/unit/conftest.py") is None
    assert unit.area_of("tests/unit/__pycache__/x.pyc") is None
    assert unit.area_of("src/app.py") is None
    assert e2e.area_of("tests/e2e/test_chat_one.py") == {"chat"}
    assert e2e.area_of("tests/e2e/test_misc.py") == {"shared"}
    assert e2e.area_of("tests/e2e/conftest.py") is None


# --- labels ----------------------------------------------------------------------------------------------------


def test_normalize_labels_lowercases_and_maps_legacy_prefix() -> None:
    assert ci_plan.normalize_labels(["E2E-Orders", "test-FULL", "bug"]) == {
        "test-orders",
        "test-full",
        "bug",
    }


def test_full_label_runs_everything(config) -> None:
    result = plan_for(config, pr(), labels=["test-full", "test-orders"])
    assert result.kind == "full" and result.areas == {
        "orders",
        "customers",
        "shared",
        "chat",
        "review",
    }


def test_area_labels_select_areas_in_both_tiers(config) -> None:
    result = plan_for(config, pr(), labels=["test-orders", "test-chat", "feature"])
    assert (result.kind, result.areas) == ("areas", {"orders", "chat"})


def test_legacy_label_counts(config) -> None:
    assert plan_for(config, pr(), labels=["e2e-customers"]).areas == {"customers"}


def test_none_label_runs_nothing(config) -> None:
    assert plan_for(config, pr(), labels=["test-none"]).kind == "none"


def test_none_label_loses_against_areas_with_a_warning(config) -> None:
    result = plan_for(config, pr(), labels=["test-none", "test-orders"])
    assert result.kind == "areas" and any(
        "test-none is ignored" in w for w in result.warnings
    )


def test_unknown_test_label_warns_and_falls_back_to_inference(config) -> None:
    result = plan_for(config, pr(), labels=["test-ordres"], files=["README.md"])
    assert result.kind == "none"
    assert any("test-ordres" in w for w in result.warnings)


def test_unreadable_labels_run_everything(config) -> None:
    result = plan_for(config, pr(), labels=OSError("timeout"))
    assert result.kind == "full" and "timeout" in result.reason


# --- inference when no label ----------------------------------------------------------------------------------------


def test_infer_empty_diff_is_none(config) -> None:
    assert plan_for(config, pr(), files=[]).kind == "none"


def test_infer_only_non_code_is_none(config) -> None:
    result = plan_for(config, pr(), files=["README.md", "docs/a/b.md"])
    assert result.kind == "none"
    assert any("no test label" in w for w in result.warnings)


def test_infer_area_tests_select_their_areas(config) -> None:
    result = plan_for(
        config,
        pr(),
        files=[
            "tests/unit/orders/test_a.py",
            "tests/e2e/test_chat_one.py",
            "README.md",
        ],
    )
    assert (result.kind, result.areas) == ("areas", {"orders", "chat"})


def test_infer_any_other_file_runs_everything(config) -> None:
    result = plan_for(config, pr(), files=["tests/unit/orders/test_a.py", "src/app.py"])
    assert result.kind == "full" and "src/app.py" in result.reason


def test_non_code_glob_is_not_a_substring_match(config) -> None:
    assert plan_for(config, pr(), files=["src/docs/x.py"]).kind == "full"
    assert plan_for(config, pr(), files=["pkg/README.md"]).kind == "full"


def test_infer_unreadable_file_list_runs_everything(config) -> None:
    assert plan_for(config, pr(), files=RuntimeError("git failed")).kind == "full"


def test_on_missing_fail_is_an_error_naming_the_choices(config) -> None:
    result = plan_for(config, pr(), on_missing="fail")
    assert (
        result.kind == "error"
        and "test-orders" in result.reason
        and "test-none" in result.reason
    )


# --- branch flow -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target"), [("dev", "test"), ("test", "main"), ("main", "dev")]
)
def test_promotions_run_nothing_without_looking_at_labels(
    config, source, target
) -> None:
    result = plan_for(config, pr(source, target), labels=OSError("must not be read"))
    assert result.kind == "none" and "promotion" in result.reason


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("dev", "main"),
        ("test", "dev"),
        ("main", "test"),
        ("feat/x", "test"),
        ("feat/x", "main"),
        ("release", "main"),
    ],
)
def test_branch_flow_violations_are_errors(config, source, target) -> None:
    assert plan_for(config, pr(source, target), labels=["test-full"]).kind == "error"


def test_fix_branch_into_main_is_tested_like_any_pr(config) -> None:
    result = plan_for(config, pr("hotfix/boom", "main"), labels=["test-orders"])
    assert (result.kind, result.areas) == ("areas", {"orders"})


def test_fix_branch_with_dev_commits_is_an_error(config) -> None:
    def violated() -> None:
        raise BranchFlowError("contains unreleased dev commits")

    result = plan_for(
        config, pr("fix/x", "main"), labels=["test-full"], fix_base=violated
    )
    assert result.kind == "error" and "unreleased dev commits" in result.reason


def test_fix_base_check_failing_to_run_only_warns(config) -> None:
    def broken() -> None:
        raise RuntimeError("shallow clone")

    result = plan_for(
        config, pr("fix/x", "main"), labels=["test-full"], fix_base=broken
    )
    assert result.kind == "full" and any("shallow clone" in w for w in result.warnings)


def test_fix_base_check_only_runs_for_main(config) -> None:
    def must_not_run() -> None:
        raise AssertionError("fix_base checked")

    assert (
        plan_for(
            config, pr("fix/x", "dev"), labels=["test-full"], fix_base=must_not_run
        ).kind
        == "full"
    )


def test_push_to_test_or_main_runs_nothing(config) -> None:
    assert plan_for(config, Context(branch="main")).kind == "none"
    assert plan_for(config, Context(branch="test")).kind == "none"


def test_nightly_dev_and_other_branches_run_everything(config) -> None:
    assert plan_for(config, Context(branch="dev")).kind == "full"
    assert plan_for(config, Context(branch="feature/x")).kind == "full"


def test_fork_runs_everything(config) -> None:
    assert plan_for(config, pr(is_fork=True), labels=["test-none"]).kind == "full"


# --- outputs ---------------------------------------------------------------------------------------------------


def test_outputs_for_selected_areas(config) -> None:
    out = ci_plan.outputs(plan_for(config, pr(), labels=["test-orders"]), config)
    assert out == {
        "runTests": "true",
        "unitRun": "true",
        "unitAreas": "orders",
        "unitPaths": "tests/unit/orders",
        "e2eRun": "false",
        "e2eAreas": "",
        "e2ePaths": "",
    }


def test_outputs_for_full_run_whole_tiers(config) -> None:
    out = ci_plan.outputs(plan_for(config, pr(), labels=["test-full"]), config)
    assert (
        out["unitPaths"] == "tests/unit"
        and out["unitAreas"] == "customers orders shared"
    )
    assert out["e2eRun"] == "true" and out["e2eAreas"] == "chat review shared"


@pytest.mark.parametrize("labels", [["test-none"]])
def test_outputs_for_none_run_nothing(config, labels) -> None:
    out = ci_plan.outputs(plan_for(config, pr(), labels=labels), config)
    assert (
        out["runTests"] == "false"
        and out["unitRun"] == "false"
        and out["e2ePaths"] == ""
    )
