import pytest

from bmsdna.devtools.pr_create_args import PrCreateOptions
from bmsdna.devtools.pr_merge_defaults import DEFAULT_LONG_LIVED_BRANCHES, apply_merge_defaults, long_lived_branches


def test_long_lived_branches_default_without_pyproject(tmp_path) -> None:
    assert long_lived_branches(tmp_path) == DEFAULT_LONG_LIVED_BRANCHES


def test_long_lived_branches_reads_pyproject(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.pr]\nlong_lived_branches = ["trunk", "release"]\n')
    assert long_lived_branches(tmp_path) == ("trunk", "release")


def test_long_lived_branches_may_be_emptied(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.bdt.pr]\nlong_lived_branches = []\n")
    assert long_lived_branches(tmp_path) == ()


@pytest.mark.parametrize("raw", ['"dev"', "[1, 2]"])
def test_long_lived_branches_ignores_malformed_value(tmp_path, raw: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f"[tool.bdt.pr]\nlong_lived_branches = {raw}\n")
    assert long_lived_branches(tmp_path) == DEFAULT_LONG_LIVED_BRANCHES


def test_feature_branch_on_ado_squashes_and_deletes_source() -> None:
    opts = PrCreateOptions()
    apply_merge_defaults(opts, "ado", "feature-x", DEFAULT_LONG_LIVED_BRANCHES)
    assert (opts.squash, opts.delete_source_branch) == (True, True)


@pytest.mark.parametrize("source", DEFAULT_LONG_LIVED_BRANCHES)
def test_long_lived_source_keeps_commits_and_branch(source: str) -> None:
    opts = PrCreateOptions()
    apply_merge_defaults(opts, "ado", source, DEFAULT_LONG_LIVED_BRANCHES)
    assert (opts.squash, opts.delete_source_branch) == (None, None)


def test_explicit_flags_win_over_the_defaults() -> None:
    opts = PrCreateOptions(squash=False, delete_source_branch=False)
    apply_merge_defaults(opts, "ado", "feature-x", DEFAULT_LONG_LIVED_BRANCHES)
    assert (opts.squash, opts.delete_source_branch) == (False, False)


def test_explicit_flags_on_a_long_lived_source_are_kept() -> None:
    opts = PrCreateOptions(squash=True)
    apply_merge_defaults(opts, "ado", "dev", DEFAULT_LONG_LIVED_BRANCHES)
    assert opts.squash is True


def test_github_only_defaults_squash_for_auto_merge() -> None:
    plain, auto = PrCreateOptions(), PrCreateOptions(auto_complete=True)
    apply_merge_defaults(plain, "github", "feature-x", DEFAULT_LONG_LIVED_BRANCHES)
    apply_merge_defaults(auto, "github", "feature-x", DEFAULT_LONG_LIVED_BRANCHES)
    assert (plain.squash, plain.delete_source_branch) == (None, None)
    assert (auto.squash, auto.delete_source_branch) == (True, None)


def test_configured_list_replaces_the_default() -> None:
    opts = PrCreateOptions()
    apply_merge_defaults(opts, "ado", "dev", ("trunk",))
    assert (opts.squash, opts.delete_source_branch) == (True, True)


def test_long_lived_match_ignores_case_and_ref_prefix(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.pr]\nlong_lived_branches = ["refs/heads/trunk"]\n')
    configured = long_lived_branches(tmp_path)
    assert configured == ("trunk",)
    for source in ("trunk", "Trunk"):
        opts = PrCreateOptions()
        apply_merge_defaults(opts, "ado", source, configured)
        assert (opts.squash, opts.delete_source_branch) == (None, None)
