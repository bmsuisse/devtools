import subprocess

from bmsdna.devtools.commit import commit_and_push


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
