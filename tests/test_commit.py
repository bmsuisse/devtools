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
