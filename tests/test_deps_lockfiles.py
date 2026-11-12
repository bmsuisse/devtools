import os
from pathlib import Path

import pytest

from bmsdna.devtools.deps.lockfiles import LockfileError, find_lockfiles, loads_jsonc, read_lockfile

UV_LOCK = """\
version = 1
requires-python = ">=3.12"

[[package]]
name = "app"
version = "0"
source = { virtual = "." }
dependencies = [{ name = "Requests" }]

[package.dev-dependencies]
dev = [{ name = "pytest" }]

[package.metadata]
requires-dist = [{ name = "requests", specifier = ">=2.19,<3" }]

[[package]]
name = "requests"
version = "2.19.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "idna" }]

[[package]]
name = "idna"
version = "2.7"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pytest"
version = "9.1.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "from-git"
version = "1.0"
source = { git = "https://example.invalid/x.git" }
"""

BUN_LOCK = """\
{
  "lockfileVersion": 1,
  // comments and trailing commas are valid in bun.lock
  "workspaces": {
    "": {
      "name": "t",
      "dependencies": { "@hono/node-server": "^1", "hono": "^4", },
    },
  },
  "packages": {
    "@hono/node-server": ["@hono/node-server@1.19.18", "", { "peerDependencies": { "hono": "^4" } }, "sha512-x"],
    "hono": ["hono@4.13.16", "", {}, "sha512-y"],
    "foo/hono": ["hono@3.0.0", "", {}, "sha512-z"],
    "aliased": ["aliased@npm:other@1.0.0", "", {}, "sha512-a"],
    "ws": ["ws@workspace:packages/ws"],
  },
}
"""


def test_uv_lock(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(UV_LOCK, encoding="utf-8")
    content = read_lockfile(lock)
    by_name = {p.name: p for p in content.packages}

    assert set(by_name) == {"requests", "idna", "pytest"}
    assert by_name["requests"].direct and by_name["requests"].declared == ">=2.19,<3"
    assert by_name["pytest"].direct  # dev group counts
    assert not by_name["idna"].direct and by_name["idna"].declared is None
    assert [u.name for u in content.unsupported] == ["from-git"]  # reported, not silently skipped


def test_bun_lock(tmp_path: Path) -> None:
    lock = tmp_path / "bun.lock"
    lock.write_text(BUN_LOCK, encoding="utf-8")
    content = read_lockfile(lock)
    found = {(p.name, p.version): p for p in content.packages}

    assert set(found) == {("@hono/node-server", "1.19.18"), ("hono", "4.13.16"), ("hono", "3.0.0")}
    assert found[("hono", "4.13.16")].direct and found[("hono", "4.13.16")].declared == "^4"
    assert not found[("hono", "3.0.0")].direct  # nested copy of a transitive dependency
    assert {u.name for u in content.unsupported} == {"aliased@npm:other@1.0.0", "ws@workspace:packages/ws"}


def test_jsonc_keeps_comment_markers_inside_strings() -> None:
    assert loads_jsonc('{"url": "https://x.invalid/a//b", "n": [1, 2,],}') == {
        "url": "https://x.invalid/a//b",
        "n": [1, 2],
    }


def test_unreadable_lockfile_is_an_error_not_an_empty_result(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text("this is = not [valid", encoding="utf-8")
    with pytest.raises(LockfileError):
        read_lockfile(lock)


def test_find_lockfiles_skips_vendored_and_worktree_dirs(tmp_path: Path) -> None:
    for rel in (
        "uv.lock",
        "frontend/bun.lock",
        "tests_e2e/uv.lock",
        "frontend/node_modules/x/bun.lock",
        ".venv/uv.lock",
        ".worktrees/a/uv.lock",
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    found = {p.relative_to(tmp_path.resolve()).as_posix() for p in find_lockfiles([tmp_path])}

    assert found == {"uv.lock", "frontend/bun.lock", "tests_e2e/uv.lock"}


@pytest.mark.parametrize(
    ("name", "text"), [("uv.lock", "version = 1\n"), ("bun.lock", '{"lockfileVersion": 1}'), ("bun.lock", "[]")]
)
def test_a_file_without_the_expected_sections_is_not_an_empty_project(tmp_path: Path, name: str, text: str) -> None:
    lock = tmp_path / name
    lock.write_text(text, encoding="utf-8")
    with pytest.raises(LockfileError):
        read_lockfile(lock)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read every directory")
def test_an_unreadable_directory_fails_the_scan_instead_of_hiding_lockfiles(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(LockfileError):
            find_lockfiles([tmp_path])
    finally:
        locked.chmod(0o700)
