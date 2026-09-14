import subprocess

from bmsdna.devtools.worktree import (
    OrphanedDb,
    clean_orphaned_dbs,
    clean_worktrees,
    collect_worktrees,
    find_orphaned_dbs,
    find_repos,
)


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def init_repo(path, pyproject: str | None = None):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    if pyproject is not None:
        (path / "pyproject.toml").write_text(pyproject)
    (path / "README.md").write_text("init")
    _git(["add", "."], cwd=path)
    _git(["commit", "-q", "-m", "init"], cwd=path)
    return path


def add_worktree(repo, name, base="main"):
    path = repo / ".worktrees" / name
    _git(["worktree", "add", str(path), "-b", name, base], cwd=repo)
    return path


# --- find_repos -----------------------------------------------------------


def test_find_repos_discovers_nested_repos_but_not_their_worktrees(tmp_path) -> None:
    repo_a = init_repo(tmp_path / "a")
    repo_b = init_repo(tmp_path / "nested" / "b")
    add_worktree(repo_a, "feature-x")

    repos = find_repos(tmp_path)

    assert set(repos) == {repo_a, repo_b}


def test_find_repos_skips_vendor_and_dot_directories(tmp_path) -> None:
    init_repo(tmp_path / "a")
    (tmp_path / "node_modules" / "some-pkg").mkdir(parents=True)
    init_repo(tmp_path / "node_modules" / "some-pkg" / "vendored-repo")
    (tmp_path / ".cache" / "whatever").mkdir(parents=True)
    init_repo(tmp_path / ".cache" / "whatever" / "another-repo")

    repos = find_repos(tmp_path)

    assert repos == [tmp_path / "a"]


# --- collect_worktrees / removability --------------------------------------


def test_collect_worktrees_flags_unmodified_worktree_as_merged_and_removable(tmp_path) -> None:
    repo = init_repo(tmp_path / "repo")
    add_worktree(repo, "merged-feature")

    worktrees = collect_worktrees(repo, remote="origin")
    feature = next(w for w in worktrees if w.branch == "merged-feature")

    assert feature.merged_into == ["main"]
    assert feature.removable is True


def test_collect_worktrees_flags_worktree_with_unmerged_commit_as_not_removable(tmp_path) -> None:
    repo = init_repo(tmp_path / "repo")
    path = add_worktree(repo, "wip-feature")
    (path / "new.txt").write_text("wip")
    _git(["add", "."], cwd=path)
    _git(["commit", "-q", "-m", "wip"], cwd=path)

    worktrees = collect_worktrees(repo, remote="origin")
    feature = next(w for w in worktrees if w.branch == "wip-feature")

    assert feature.merged_into == []
    assert feature.removable is False


def test_collect_worktrees_flags_dirty_worktree_as_not_removable(tmp_path) -> None:
    repo = init_repo(tmp_path / "repo")
    path = add_worktree(repo, "dirty-feature")
    (path / "uncommitted.txt").write_text("oops")

    worktrees = collect_worktrees(repo, remote="origin")
    feature = next(w for w in worktrees if w.branch == "dirty-feature")

    assert feature.dirty is True
    assert feature.removable is False


def test_collect_worktrees_never_offers_main_worktree_or_protected_branches(tmp_path) -> None:
    repo = init_repo(tmp_path / "repo")
    add_worktree(repo, "test", base="main")

    worktrees = collect_worktrees(repo, remote="origin")

    assert not any(w.removable for w in worktrees if w.is_main or w.branch == "test")


def test_collect_worktrees_computes_db_names_when_pgdevkit_configured(tmp_path) -> None:
    repo = init_repo(tmp_path / "repo", pyproject="[tool.pgdevkit]\nname = 'ccmt'\n")
    add_worktree(repo, "my-feature")

    worktrees = collect_worktrees(repo, remote="origin")
    feature = next(w for w in worktrees if w.branch == "my-feature")

    assert feature.db_names == frozenset({"ccmt_my_feature"})


def test_collect_worktrees_no_db_names_when_no_pgdevkit_config(tmp_path) -> None:
    repo = init_repo(tmp_path / "repo")
    add_worktree(repo, "my-feature")

    worktrees = collect_worktrees(repo, remote="origin")
    feature = next(w for w in worktrees if w.branch == "my-feature")

    assert feature.db_names == frozenset()


# --- clean_worktrees --------------------------------------------------------


def test_clean_worktrees_without_yes_only_previews(tmp_path, capsys) -> None:
    repo = init_repo(tmp_path / "repo")
    path = add_worktree(repo, "merged-feature")

    clean_worktrees(tmp_path, remote="origin", keep_dbs=False, yes=False, pg_port=54322, pg_user="tester")

    out = capsys.readouterr().out
    assert str(path) in out
    assert "Pass --yes to remove" in out
    assert path.exists()


def test_clean_worktrees_with_yes_removes_merged_worktree_and_drops_its_db(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "repo", pyproject="[tool.pgdevkit]\nname = 'ccmt'\n")
    path = add_worktree(repo, "merged-feature")

    dropped: list[str] = []
    monkeypatch.setattr(
        "bmsdna.devtools.worktree.testdb.drop_database",
        lambda name, pg_port, pg_user: (dropped.append(name), subprocess.CompletedProcess([], 0))[1],
    )

    clean_worktrees(tmp_path, remote="origin", keep_dbs=False, yes=True, pg_port=54322, pg_user="tester")

    out = capsys.readouterr().out
    assert not path.exists()
    assert dropped == ["ccmt_merged_feature"]
    assert "dropped db ccmt_merged_feature" in out


def test_clean_worktrees_keep_dbs_skips_db_drop(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path / "repo", pyproject="[tool.pgdevkit]\nname = 'ccmt'\n")
    add_worktree(repo, "merged-feature")

    dropped: list[str] = []
    monkeypatch.setattr(
        "bmsdna.devtools.worktree.testdb.drop_database",
        lambda name, pg_port, pg_user: (dropped.append(name), subprocess.CompletedProcess([], 0))[1],
    )

    clean_worktrees(tmp_path, remote="origin", keep_dbs=True, yes=True, pg_port=54322, pg_user="tester")

    assert dropped == []


def test_clean_worktrees_leaves_unmerged_worktree_alone(tmp_path) -> None:
    repo = init_repo(tmp_path / "repo")
    path = add_worktree(repo, "wip-feature")
    (path / "new.txt").write_text("wip")
    _git(["add", "."], cwd=path)
    _git(["commit", "-q", "-m", "wip"], cwd=path)

    clean_worktrees(tmp_path, remote="origin", keep_dbs=False, yes=True, pg_port=54322, pg_user="tester")

    assert path.exists()


def test_clean_worktrees_reports_nothing_to_do_when_no_repos(tmp_path, capsys) -> None:
    clean_worktrees(tmp_path, remote="origin", keep_dbs=False, yes=True, pg_port=54322, pg_user="tester")

    assert "No git repositories found" in capsys.readouterr().out


# --- find_orphaned_dbs / clean_orphaned_dbs --------------------------------
#
# testdb.find_orphaned() (which these delegate to) is exercised directly and
# more thoroughly in test_testdb.py, including the pgdevkit-per-project-root
# fan-out; these tests only cover find_repos-driven discovery across
# multiple repos and the preview/drop/--include-caution CLI-flow behavior on
# top of it, via a monkeypatched testdb.find_orphaned.


def _fake_find_orphaned(by_repo: dict) -> object:
    """Monkeypatch bmsdna.devtools.worktree.testdb.find_orphaned to return
    `by_repo[repo]` (a list of (name, project, caution) tuples) for each
    repo, [] for any repo not in by_repo."""
    return lambda repo: by_repo.get(repo, [])


def test_find_orphaned_dbs_fans_out_across_every_discovered_repo(tmp_path, monkeypatch) -> None:
    repo_a = init_repo(tmp_path / "a", pyproject="[tool.pgdevkit]\nname = 'ccmt'\n")
    repo_b = init_repo(tmp_path / "b", pyproject="[tool.pgdevkit]\nname = 'mdm'\n")

    monkeypatch.setattr(
        "bmsdna.devtools.worktree.testdb.find_orphaned",
        _fake_find_orphaned(
            {
                repo_a: [("ccmt_old_removed_feature", "ccmt", False)],
                repo_b: [("mdm_dev", "mdm", True)],
            }
        ),
    )

    orphaned = find_orphaned_dbs(tmp_path)

    assert sorted(orphaned, key=lambda o: o.name) == [
        OrphanedDb("ccmt_old_removed_feature", "ccmt", False),
        OrphanedDb("mdm_dev", "mdm", True),
    ]


def test_clean_orphaned_dbs_without_yes_only_previews_and_excludes_caution_by_default(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "repo", pyproject="[tool.pgdevkit]\nname = 'ccmt'\n")

    monkeypatch.setattr(
        "bmsdna.devtools.worktree.testdb.find_orphaned",
        _fake_find_orphaned({repo: [("ccmt_old_removed_feature", "ccmt", False), ("ccmt_dev", "ccmt", True)]}),
    )
    dropped: list[str] = []
    monkeypatch.setattr(
        "bmsdna.devtools.worktree.testdb.drop_database",
        lambda name, pg_port, pg_user: (dropped.append(name), subprocess.CompletedProcess([], 0))[1],
    )

    clean_orphaned_dbs(tmp_path, include_caution=False, yes=False, pg_port=54322, pg_user="tester")

    out = capsys.readouterr().out
    assert "ccmt_old_removed_feature" in out
    assert "ccmt_dev" in out
    assert "possibly a standing reference DB" in out
    assert "Pass --yes to drop" in out
    assert dropped == []


def test_clean_orphaned_dbs_with_yes_drops_only_non_caution_by_default(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path / "repo", pyproject="[tool.pgdevkit]\nname = 'ccmt'\n")

    monkeypatch.setattr(
        "bmsdna.devtools.worktree.testdb.find_orphaned",
        _fake_find_orphaned({repo: [("ccmt_old_removed_feature", "ccmt", False), ("ccmt_dev", "ccmt", True)]}),
    )
    dropped: list[str] = []
    monkeypatch.setattr(
        "bmsdna.devtools.worktree.testdb.drop_database",
        lambda name, pg_port, pg_user: (dropped.append(name), subprocess.CompletedProcess([], 0))[1],
    )

    clean_orphaned_dbs(tmp_path, include_caution=False, yes=True, pg_port=54322, pg_user="tester")

    assert dropped == ["ccmt_old_removed_feature"]


def test_clean_orphaned_dbs_with_yes_and_include_caution_drops_everything(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path / "repo", pyproject="[tool.pgdevkit]\nname = 'ccmt'\n")

    monkeypatch.setattr(
        "bmsdna.devtools.worktree.testdb.find_orphaned",
        _fake_find_orphaned({repo: [("ccmt_old_removed_feature", "ccmt", False), ("ccmt_dev", "ccmt", True)]}),
    )
    dropped: list[str] = []
    monkeypatch.setattr(
        "bmsdna.devtools.worktree.testdb.drop_database",
        lambda name, pg_port, pg_user: (dropped.append(name), subprocess.CompletedProcess([], 0))[1],
    )

    clean_orphaned_dbs(tmp_path, include_caution=True, yes=True, pg_port=54322, pg_user="tester")

    assert sorted(dropped) == ["ccmt_dev", "ccmt_old_removed_feature"]


def test_clean_orphaned_dbs_reports_none_found(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "repo", pyproject="[tool.pgdevkit]\nname = 'ccmt'\n")
    monkeypatch.setattr("bmsdna.devtools.worktree.testdb.find_orphaned", _fake_find_orphaned({repo: []}))

    clean_orphaned_dbs(tmp_path, include_caution=False, yes=True, pg_port=54322, pg_user="tester")

    assert "No orphaned pgdevkit test DBs found" in capsys.readouterr().out


def test_clean_orphaned_dbs_reports_no_repos_found_distinctly_from_no_orphans(tmp_path, capsys) -> None:
    """A typo'd/empty root (no repos at all) must be reported distinctly from
    a scan that ran but found nothing -- otherwise a mistaken --root value
    silently looks identical to a clean sweep."""
    clean_orphaned_dbs(tmp_path, include_caution=False, yes=True, pg_port=54322, pg_user="tester")

    out = capsys.readouterr().out
    assert "No git repositories found" in out
    assert "No orphaned pgdevkit test DBs found" not in out
