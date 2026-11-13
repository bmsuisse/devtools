import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bmsdna.devtools.deps.cooldown import changed_packages, check
from bmsdna.devtools.deps.lockfiles import LockedPackage, LockfileError, read_lockfile
from bmsdna.devtools.deps.osv import DataError

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def pkg(name: str, version: str = "1.0") -> LockedPackage:
    return LockedPackage("Maven", name, version, True, Path("gradle/libs.versions.toml"))


def test_check_flags_versions_inside_the_cooldown_and_honours_exemptions() -> None:
    released = {
        "g:old": NOW - timedelta(days=30),
        "g:young": NOW - timedelta(days=2),
        "g:edge": NOW - timedelta(days=5),
        "g:fix": NOW - timedelta(days=1),
        "g:private": None,
    }
    result = check(
        [pkg(n) for n in released],
        lambda _eco, name, _version: released[name],
        cooldown_days=5,
        exempt=frozenset({"g:fix"}),
        now=NOW,
    )

    assert [y.package.name for y in result.young] == ["g:young"] and result.young[0].age_days == 2.0
    assert [y.package.name for y in result.exempt] == ["g:fix"]
    assert [p.name for p in result.unknown] == ["g:private"]  # listed, never a failure and never "fine"
    assert result.checked == 5 and not result.incomplete


def test_check_reports_an_unreadable_registry_as_incomplete_not_as_clean() -> None:
    def down(_eco: str, _name: str, _version: str) -> datetime | None:
        raise DataError("registry down")

    result = check([pkg("g:a")], down, cooldown_days=5, exempt=frozenset(), now=NOW)

    assert result.incomplete and not result.young


CATALOG = '[libraries]\nokhttp = "com.squareup.okhttp3:okhttp:{okhttp}"\nokio = "com.squareup.okio:okio:3.9.0"\n'


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "gradle").mkdir()
    (tmp_path / "gradle" / "libs.versions.toml").write_text(CATALOG.format(okhttp="4.12.0"), encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_changed_packages_are_the_versions_a_branch_adds_or_changes(repo: Path) -> None:
    catalog = repo / "gradle" / "libs.versions.toml"
    catalog.write_text(CATALOG.format(okhttp="5.0.0") + 'new = "org.example:new:1.0"\n', encoding="utf-8")

    changed = changed_packages(read_lockfile(catalog), "HEAD")

    assert {(p.name, p.version) for p in changed} == {
        ("com.squareup.okhttp3:okhttp", "5.0.0"),
        ("org.example:new", "1.0"),
    }  # okio is unchanged: an old pin never fails a later pull request


def test_a_file_that_is_new_on_the_branch_is_checked_completely(repo: Path) -> None:
    other = repo / "app" / "gradle"
    other.mkdir(parents=True)
    (other / "libs.versions.toml").write_text(CATALOG.format(okhttp="4.12.0"), encoding="utf-8")

    assert len(changed_packages(read_lockfile(other / "libs.versions.toml"), "HEAD")) == 2


def test_an_unknown_base_revision_is_a_setup_error(repo: Path) -> None:
    with pytest.raises(LockfileError):
        changed_packages(read_lockfile(repo / "gradle" / "libs.versions.toml"), "no-such-ref")
