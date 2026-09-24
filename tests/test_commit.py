import shutil
import subprocess

import pytest

from bmsdna.devtools.commit import (
    allowed_commit_scopes,
    allowed_commit_types,
    commit_and_push,
    conventional_commit_scope,
    conventional_commit_type,
)


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


@pytest.mark.parametrize(
    "message,expected",
    [
        ("feat(x): add widget", "feat"),
        ("fix: correct off-by-one", "fix"),
        ("feat!: breaking change", "feat"),
        ("feat(x)!: breaking change with scope", "feat"),
        ("chore(deps): bump requests", "chore"),
        ("not a conventional message", None),
        ("feat missing colon", None),
        ("feat:missing space", None),
        ("", None),
    ],
)
def test_conventional_commit_type(message, expected) -> None:
    assert conventional_commit_type(message) == expected


def test_allowed_commit_types_includes_builtins_with_no_pyproject(tmp_path) -> None:
    types = allowed_commit_types(tmp_path)
    assert {"feat", "fix", "chore"} <= types


def test_allowed_commit_types_extends_with_pyproject_config(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.commit]\ntypes = ["sql", "infra"]\n')
    types = allowed_commit_types(tmp_path)
    assert {"feat", "fix", "sql", "infra"} <= types


def test_commit_and_push_rejects_non_conventional_message(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "just a plain message that is long enough",
        ["a.txt"],
        require_feature_branch=False,
    )

    assert result.committed is False
    assert result.success is False
    assert "Conventional Commits" in (result.error or "")


def test_commit_and_push_accepts_custom_type_from_pyproject(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.commit]\ntypes = ["sql"]\n')
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "sql(migrations): add users table",
        ["a.txt", "pyproject.toml"],
        require_feature_branch=False,
    )

    assert result.committed is True
    assert result.extra.get("commit_type") == "sql"


def test_commit_and_push_records_feat_commit_type_in_extra(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "feat(x): add a.txt",
        ["a.txt"],
        require_feature_branch=False,
    )

    assert result.committed is True
    assert result.extra.get("commit_type") == "feat"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("feat(x): add widget", "x"),
        ("fix: correct off-by-one", None),
        ("feat(x)!: breaking change with scope", "x"),
        ("not a conventional message", None),
    ],
)
def test_conventional_commit_scope(message, expected) -> None:
    assert conventional_commit_scope(message) == expected


def test_allowed_commit_scopes_empty_with_no_pyproject(tmp_path) -> None:
    assert allowed_commit_scopes(tmp_path) == set()


def test_allowed_commit_scopes_reads_pyproject_config(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.commit]\nscopes = ["api", "ui"]\n')
    assert allowed_commit_scopes(tmp_path) == {"api", "ui"}


def test_commit_and_push_allows_any_scope_when_unconfigured(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "feat(whatever): add a.txt",
        ["a.txt"],
        require_feature_branch=False,
    )

    assert result.committed is True


def test_commit_and_push_allows_no_scope_even_when_scopes_configured(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.commit]\nscopes = ["api"]\n')
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "feat: add a.txt",
        ["a.txt", "pyproject.toml"],
        require_feature_branch=False,
    )

    assert result.committed is True


def test_commit_and_push_accepts_configured_scope(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.commit]\nscopes = ["api"]\n')
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "feat(api): add a.txt",
        ["a.txt", "pyproject.toml"],
        require_feature_branch=False,
    )

    assert result.committed is True
    assert result.extra.get("commit_scope") == "api"


def test_commit_and_push_rejects_scope_outside_configured_list(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.commit]\nscopes = ["api"]\n')
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "feat(bogus): add a.txt",
        ["a.txt", "pyproject.toml"],
        require_feature_branch=False,
    )

    assert result.committed is False
    assert result.success is False
    assert "scope" in (result.error or "").lower()


def test_commit_and_push_skips_scope_check_with_skip_message_check(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "pyproject.toml").write_text('[tool.bdt.commit]\nscopes = ["api"]\n')
    monkeypatch.chdir(tmp_path)

    result = commit_and_push(
        "feat(bogus): add a.txt",
        ["a.txt", "pyproject.toml"],
        require_message_quality=False,
        require_feature_branch=False,
    )

    assert result.committed is True
