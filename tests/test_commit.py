import os
import shutil
import subprocess

import pytest

from bmsdna.devtools.commit import _pre_commit_hook_installed, commit_and_push


def init_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "feature"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def test_commit_and_push_fails_when_push_has_no_remote(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "feat(x): add a.txt",
        ["a.txt"],
        require_message_quality=False,
        require_feature_branch=False,
    )

    assert result.committed is True
    assert result.pushed is False
    assert result.success is False
    assert result.error


def test_commit_and_push_allows_staged_deletion(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    subprocess.run(["git", "rm", "-q", "a.txt"], cwd=tmp_path, check=True)

    result = commit_and_push(
        "feat(x): remove a.txt",
        ["a.txt"],
        require_message_quality=False,
        require_feature_branch=False,
    )

    assert result.committed is True
    assert result.error != "File not found: a.txt — did you typo the path? Run `git status` to see changed files"


def test_commit_and_push_rejects_unstaged_missing_file(tmp_path, monkeypatch):
    """A file that vanished from disk without git being told (e.g. a failed
    write) must still be rejected -- only an already-*staged* deletion (via
    `git rm`) is treated as an intentional removal."""
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    (tmp_path / "a.txt").unlink()

    result = commit_and_push(
        "feat(x): update a.txt",
        ["a.txt"],
        require_message_quality=False,
        require_feature_branch=False,
    )

    assert result.committed is False
    assert result.error == "File not found: a.txt — did you typo the path? Run `git status` to see changed files"


def test_commit_and_push_stages_modified_file_alongside_staged_deletion(tmp_path, monkeypatch):
    """Regression test: `git add <modified-file> <already-git-rm'd-file>` fails
    its ENTIRE invocation (git errors "pathspec did not match any files" for
    the already-removed path, staging nothing at all in that call) -- which
    previously silently dropped the modified file's new content from the
    commit, since _commit_with_retry didn't check git add's exit code."""
    init_repo(tmp_path)
    (tmp_path / "a.md").write_text("old")
    (tmp_path / "gone.py").write_text("old")
    subprocess.run(["git", "add", "a.md", "gone.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)

    subprocess.run(["git", "rm", "-q", "gone.py"], cwd=tmp_path, check=True)
    (tmp_path / "a.md").write_text("new content")

    result = commit_and_push(
        "feat(x): update a.md and remove gone.py",
        ["a.md", "gone.py"],
        require_message_quality=False,
        require_feature_branch=False,
    )

    assert result.committed is True
    committed_content = subprocess.run(
        ["git", "show", "HEAD:a.md"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert committed_content == "new content"


def test_commit_and_push_allows_staged_deletion_in_subrepo(tmp_path, monkeypatch):
    subrepo = tmp_path / "database"
    subrepo.mkdir()
    init_repo(subrepo)
    (subrepo / "schema.sql").write_text("create table t();")
    subprocess.run(["git", "add", "schema.sql"], cwd=subrepo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=subrepo, check=True)

    init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init", "--allow-empty"], cwd=tmp_path, check=True)
    subprocess.run(["git", "rm", "-q", "schema.sql"], cwd=subrepo, check=True)

    result = commit_and_push(
        "feat(x): remove schema.sql",
        ["database/schema.sql"],
        require_message_quality=False,
        require_feature_branch=False,
        subrepos=["database"],
    )

    assert result.error != "File not found: database/schema.sql — did you typo the path? Run `git status` to see changed files"


def test_commit_and_push_warns_on_no_verify(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "feat(x): add a.txt",
        ["a.txt"],
        no_verify=True,
        require_message_quality=False,
        require_feature_branch=False,
    )

    assert result.committed is True
    assert any("--no-verify" in w for w in result.warnings)


def test_commit_and_push_no_verify_skips_prek_hook_install(tmp_path, monkeypatch):
    """Even if a prek.toml is sitting there with no hook installed, --no-verify
    means skip all verification -- the hook must not get installed either."""
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "prek.toml").write_text("")
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "feat(x): add a.txt",
        ["a.txt", "prek.toml"],
        no_verify=True,
        require_message_quality=False,
        require_feature_branch=False,
    )

    assert result.committed is True
    assert not any("prek" in w for w in result.warnings)
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()


def test_pre_commit_hook_installed_from_linked_worktree(tmp_path, monkeypatch):
    """Regression test: in a linked worktree, `git rev-parse --git-dir`
    returns the worktree-private admin dir (e.g. `.git/worktrees/<name>`),
    which has no `hooks/` of its own -- the shared hooks live under the repo
    found via `--git-common-dir`. Detection must use the common dir, or a
    hook that's installed in the main checkout is reported as "not
    installed" every time a command runs from inside the worktree."""
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    init_repo(main_repo)
    (main_repo / "a.txt").write_text("hello")
    subprocess.run(["git", "add", "a.txt"], cwd=main_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=main_repo, check=True)

    hooks_dir = main_repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text("#!/bin/sh\nexit 0\n")
    hook_path.chmod(0o755)

    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "wt-branch", str(worktree)],
        cwd=main_repo,
        check=True,
    )

    # Sanity check on the premise: --git-dir and --git-common-dir genuinely
    # differ inside the linked worktree, and only the common dir has hooks/.
    git_dir = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=worktree, capture_output=True, text=True, check=True
    ).stdout.strip()
    common_dir = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"], cwd=worktree, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert git_dir != common_dir
    assert not os.path.isdir(os.path.join(worktree, git_dir, "hooks"))

    assert _pre_commit_hook_installed(str(worktree)) is True


def test_hooks_dir_prefers_core_hooks_path(tmp_path, monkeypatch):
    """If a repo sets `core.hooksPath`, hooks live there instead of under
    `<git-common-dir>/hooks` -- that override must be respected."""
    init_repo(tmp_path)
    custom_hooks = tmp_path / "custom-hooks"
    custom_hooks.mkdir()
    subprocess.run(["git", "config", "core.hooksPath", str(custom_hooks)], cwd=tmp_path, check=True)
    hook_path = custom_hooks / "pre-commit"
    hook_path.write_text("#!/bin/sh\nexit 0\n")
    hook_path.chmod(0o755)

    assert _pre_commit_hook_installed(str(tmp_path)) is True


@pytest.mark.skipif(shutil.which("prek") is None, reason="prek is not installed")
def test_commit_and_push_installs_prek_hook_when_missing(tmp_path, monkeypatch):
    """A prek.toml with no pre-commit hook installed means `git commit` alone
    would silently skip the checks it configures -- install the hook so git's
    normal mechanism picks it up, the regular way."""
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "prek.toml").write_text(
        '[[repos]]\nrepo = "local"\nhooks = [{ id = "always-fail", name = "always-fail", entry = "false", language = "system" }]\n'
    )
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "feat(x): add a.txt",
        ["a.txt", "prek.toml"],
        require_message_quality=False,
        require_feature_branch=False,
    )

    assert (tmp_path / ".git" / "hooks" / "pre-commit").exists()
    # The now-installed hook runs via git's normal commit flow and fails,
    # same as any other pre-commit hook failure -- not a `warnings` entry.
    assert result.committed is False
    assert result.error
