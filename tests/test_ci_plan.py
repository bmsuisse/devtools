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


def area_of(tier: Tier, root, path: str):
    return tier.area_of(root, path, tier.areas(root))


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
    root = config.root
    assert area_of(unit, root, "tests/unit/orders/test_a.py") == {"orders"}
    assert area_of(unit, root, "tests/unit/conftest.py") is None
    assert area_of(unit, root, "tests/unit/__pycache__/x.pyc") is None
    assert area_of(unit, root, "src/app.py") is None
    assert area_of(e2e, root, "tests/e2e/test_chat_one.py") == {"chat"}
    assert area_of(e2e, root, "tests/e2e/test_misc.py") == {"shared"}
    assert area_of(e2e, root, "tests/e2e/conftest.py") is None


def test_folders_without_tests_are_not_areas(config) -> None:
    touch(
        config.root, "tests/unit/test-results/report.xml", "tests/unit/helpers/util.py"
    )
    unit = config.tiers[0]
    assert unit.areas(config.root) == ["customers", "orders", "shared"]
    assert area_of(unit, config.root, "tests/unit/helpers/util.py") is None
    assert area_of(unit, config.root, "tests/unit/test-results/report.xml") is None


def test_area_folder_helpers_select_their_area(config) -> None:
    touch(
        config.root,
        "tests/unit/orders/conftest.py",
        "tests/unit/orders/fixtures/data.json",
    )
    unit = config.tiers[0]
    assert area_of(unit, config.root, "tests/unit/orders/conftest.py") == {"orders"}
    assert area_of(unit, config.root, "tests/unit/orders/fixtures/data.json") == {
        "orders"
    }


def test_nested_and_suffix_style_tests_make_an_area(tmp_path) -> None:
    touch(tmp_path, "tests/unit/billing/deep/er/orders_test.py")
    assert Tier("unit", "tests/unit").areas(tmp_path) == ["billing"]


def test_removed_area_test_file_still_names_the_area(config) -> None:
    unit = config.tiers[0]
    assert area_of(unit, config.root, "tests/unit/legacy/test_x.py") == {"legacy"}
    assert area_of(unit, config.root, "tests/unit/legacy/helper.py") is None


def test_changed_helper_folder_file_runs_everything(config) -> None:
    touch(config.root, "tests/unit/helpers/util.py")
    result = plan_for(config, pr(), files=["tests/unit/helpers/util.py"])
    assert result.kind == "full" and "tests/unit/helpers/util.py" in result.reason


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
    for branch in ("main", "test"):
        for reason in ("IndividualCI", "BatchedCI"):
            assert (
                plan_for(config, Context(branch=branch, reason=reason)).kind == "none"
            )


@pytest.mark.parametrize("branch", ["main", "test"])
@pytest.mark.parametrize("reason", ["Manual", "Schedule", ""])
def test_manual_and_scheduled_runs_on_test_or_main_run_everything(
    config, branch, reason
) -> None:
    assert plan_for(config, Context(branch=branch, reason=reason)).kind == "full"


def test_nightly_dev_and_other_branches_run_everything(config) -> None:
    assert plan_for(config, Context(branch="dev", reason="Schedule")).kind == "full"
    assert (
        plan_for(config, Context(branch="feature/x", reason="IndividualCI")).kind
        == "full"
    )


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


def test_area_named_like_a_control_label_is_rejected(tmp_path) -> None:
    touch(tmp_path, "tests/unit/none/test_a.py")
    (tmp_path / "pyproject.toml").write_text("[tool.bdt.ci.plan]\n")
    with pytest.raises(ValueError, match="collide"):
        load_plan_config(tmp_path)


def test_paths_with_spaces_are_shell_quoted(tmp_path) -> None:
    touch(tmp_path, "tests/unit/customer orders/test_a.py")
    (tmp_path / "pyproject.toml").write_text("[tool.bdt.ci.plan]\n")
    config = load_plan_config(tmp_path)
    assert config is not None
    result = Plan("areas", "x", frozenset({"customer orders"}))
    assert (
        ci_plan.outputs(result, config)["unitPaths"] == "'tests/unit/customer orders'"
    )


def test_removed_area_tests_leave_nothing_to_run(config) -> None:
    result = plan_for(config, pr(), files=["tests/unit/legacy/test_x.py"])
    assert result.kind == "none" and "no longer exist" in result.reason


def test_non_code_star_is_root_only_and_double_star_is_recursive(config) -> None:
    root_only = PlanConfig(config.root, config.tiers, "infer", ("*.md",))
    recursive = PlanConfig(config.root, config.tiers, "infer", ("**/*.md",))
    assert root_only.is_non_code("README.md") and not root_only.is_non_code(
        "src/pkg/README.md"
    )
    assert recursive.is_non_code("README.md") and recursive.is_non_code(
        "src/pkg/README.md"
    )


def test_a_directory_named_like_a_test_file_is_not_a_test(tmp_path) -> None:
    touch(
        tmp_path,
        "tests/unit/helpers/test_fixture.py/data.json",
        "tests/unit/helpers/util.py",
    )
    tier = Tier("unit", "tests/unit")
    assert tier.areas(tmp_path) == []
    assert area_of(tier, tmp_path, "tests/unit/helpers/util.py") is None


def test_pycache_in_the_checkout_path_does_not_hide_areas(tmp_path) -> None:
    root = tmp_path / "__pycache__" / "repo"
    touch(
        root, "tests/unit/orders/test_a.py", "tests/unit/orders/__pycache__/test_a.py"
    )
    assert Tier("unit", "tests/unit").areas(root) == ["orders"]
    touch(root, "tests/unit/cached/__pycache__/test_b.py")
    assert Tier("unit", "tests/unit").areas(root) == ["orders"]


# --- areas that own paths (infra) -----------------------------------------------------------------------------


def infra_config(tmp_path, *, infra_dir: bool = True, extra: str = "") -> PlanConfig:
    touch(tmp_path, "tests/unit/orders/test_a.py")
    if infra_dir:
        touch(tmp_path, "infra/main.bicep", "infra/modules/app.bicep")
    (tmp_path / "pyproject.toml").write_text(f"[tool.bdt.ci.plan]\n{extra}")
    config = load_plan_config(tmp_path)
    assert config is not None
    return config


def test_infra_area_is_built_in_when_an_infra_folder_exists(tmp_path) -> None:
    config = infra_config(tmp_path)
    assert config.paths == {"infra": ("infra/**",)}
    assert "infra" in config.all_areas()
    assert "test-infra" in ci_plan.required_label_choices(config)


def test_no_infra_area_without_an_infra_folder(tmp_path) -> None:
    config = infra_config(tmp_path, infra_dir=False)
    assert config.paths == {} and "infra" not in config.all_areas()


def test_paths_table_overrides_and_extends(tmp_path) -> None:
    config = infra_config(
        tmp_path,
        extra='[tool.bdt.ci.plan.paths]\ninfra = ["deploy/**"]\nfrontend = ["web/**"]\n',
    )
    assert config.paths == {"infra": ("deploy/**",), "frontend": ("web/**",)}


def test_invalid_paths_config_is_rejected(tmp_path) -> None:
    touch(tmp_path, "tests/unit/orders/test_a.py")
    (tmp_path / "pyproject.toml").write_text("[tool.bdt.ci.plan]\npaths = 3\n")
    with pytest.raises(ValueError, match="paths"):
        load_plan_config(tmp_path)


def test_infra_only_pr_infers_the_infra_area_and_runs_no_tier(tmp_path) -> None:
    config = infra_config(tmp_path)
    result = plan_for(
        config, pr(), files=["infra/main.bicep", "infra/modules/app.bicep"]
    )
    assert (result.kind, result.areas) == ("areas", {"infra"})
    out = ci_plan.outputs(result, config)
    assert (
        out["runTests"] == "true"
        and out["unitRun"] == "false"
        and out["unitPaths"] == ""
    )


def test_infra_change_next_to_code_still_runs_everything(tmp_path) -> None:
    config = infra_config(tmp_path)
    assert (
        plan_for(config, pr(), files=["infra/main.bicep", "src/app.py"]).kind == "full"
    )


def test_infra_label_selects_the_infra_area(tmp_path) -> None:
    config = infra_config(tmp_path)
    result = plan_for(config, pr(), labels=["test-infra"])
    assert (result.kind, result.areas) == ("areas", {"infra"})


def test_infra_label_runs_the_infra_tests_when_the_repo_has_some(tmp_path) -> None:
    touch(tmp_path, "tests/unit/infra/test_bicep.py")
    config = infra_config(tmp_path)
    result = plan_for(config, pr(), labels=["test-infra"])
    assert ci_plan.outputs(result, config)["unitPaths"] == "tests/unit/infra"
    changed = plan_for(config, pr(), files=["infra/main.bicep"])
    assert ci_plan.outputs(changed, config)["unitPaths"] == "tests/unit/infra"


def test_infra_label_without_an_infra_folder_is_unknown(tmp_path) -> None:
    config = infra_config(tmp_path, infra_dir=False)
    result = plan_for(config, pr(), labels=["test-infra"], files=[])
    assert result.kind == "none" and any("test-infra" in w for w in result.warnings)


def test_path_area_named_like_a_control_label_is_rejected(tmp_path) -> None:
    touch(tmp_path, "tests/unit/orders/test_a.py")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.bdt.ci.plan]\n[tool.bdt.ci.plan.paths]\nnone = ["x/**"]\n'
    )
    with pytest.raises(ValueError, match="collide"):
        load_plan_config(tmp_path)
