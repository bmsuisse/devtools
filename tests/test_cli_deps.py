import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.deps import cli as deps_cli
from bmsdna.devtools.deps.osv import DataError

runner = CliRunner()

UV_LOCK = """\
version = 1

[[package]]
name = "app"
version = "0"
source = { virtual = "." }
dependencies = [{ name = "lodash-py" }]

[[package]]
name = "lodash-py"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "from-git"
version = "1.0"
source = { git = "https://example.invalid/x.git" }
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "uv.lock").write_text(UV_LOCK, encoding="utf-8")
    return tmp_path


def test_affected_exit_codes(project: Path) -> None:
    hit = runner.invoke(app, ["deps", "affected", "Lodash_Py", "--version-spec", "<2", "--path", str(project)])
    assert hit.exit_code == 1 and "AFFECTED" in hit.output and "(direct)" in hit.output

    miss = runner.invoke(app, ["deps", "affected", "lodash-py", "--version-spec", ">=2", "--path", str(project)])
    assert miss.exit_code == 0

    unknown_source = runner.invoke(app, ["deps", "affected", "from-git", "--path", str(project)])
    assert (
        unknown_source.exit_code == 3 and "UNKNOWN" in unknown_source.output
    )  # never "not affected" for what we can't match


def test_setup_errors_exit_2(tmp_path: Path) -> None:
    assert runner.invoke(app, ["deps", "affected", "x", "--path", str(tmp_path)]).exit_code == 2  # no lockfile
    assert runner.invoke(app, ["deps", "affected", "x", "--path", str(tmp_path / "missing")]).exit_code == 2
    bad_spec = runner.invoke(app, ["deps", "affected", "x", "--version-spec", "~~", "--path", str(tmp_path)])
    assert bad_spec.exit_code == 2


class FakeClient:
    """Stands in for OsvClient: lodash-py 1.0.0 has one high advisory fixed in 2.0.0, published long ago."""

    fail = False

    def query_batch(self, packages: Any) -> dict:
        if self.fail:
            raise DataError("OSV down")
        return {p: (["GHSA-1"] if p[1] == "lodash-py" else []) for p in packages}

    def vuln(self, vuln_id: str) -> dict:
        return {
            "id": vuln_id,
            "database_specific": {"severity": "HIGH"},
            "affected": [
                {
                    "package": {"ecosystem": "PyPI", "name": "lodash-py"},
                    "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.0.0"}]}],
                }
            ],
        }

    def kev_cves(self) -> set[str]:
        return set()

    def release_time(self, ecosystem: str, name: str, version: str) -> None:
        return None


def test_advisories_reports_findings_unsupported_and_exits_1(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps_cli, "OsvClient", FakeClient)

    result = runner.invoke(app, ["deps", "advisories", "--path", str(project)])

    assert result.exit_code == 1, result.output
    assert "[exempt-eligible] lodash-py 1.0.0" in result.output and "-> 2.0.0" in result.output
    assert "[unsupported] from-git" in result.output

    as_json = runner.invoke(app, ["deps", "advisories", "--json", "--path", str(project)])
    assert json.loads(as_json.output)["findings"][0]["status"] == "exempt-eligible"


def test_advisories_fails_closed_when_osv_is_down(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Down(FakeClient):
        fail = True

    monkeypatch.setattr(deps_cli, "OsvClient", Down)

    result = runner.invoke(app, ["deps", "advisories", "--path", str(project)])

    assert result.exit_code == 3
    assert "no advisory" not in result.output


def test_unsupported_sources_are_listed_but_do_not_change_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "uv.lock").write_text(UV_LOCK.replace("lodash-py", "other"), encoding="utf-8")
    monkeypatch.setattr(deps_cli, "OsvClient", FakeClient)

    result = runner.invoke(app, ["deps", "advisories", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "[unsupported] from-git" in result.output


CATALOG = '[libraries]\nokhttp = "com.squareup.okhttp3:okhttp:4.12.0"\nokio = "com.squareup.okio:okio:3.9.0"\n'


@pytest.fixture
def gradle_project(tmp_path: Path) -> Path:
    (tmp_path / "gradle").mkdir()
    (tmp_path / "gradle" / "libs.versions.toml").write_text(CATALOG, encoding="utf-8")
    return tmp_path


class YoungClient(FakeClient):
    """okhttp 4.12.0 is two days old and has a high advisory fixed in 5.0.0, okio is old."""

    def query_batch(self, packages: Any) -> dict:
        return {p: (["GHSA-2"] if p[1].endswith(":okhttp") else []) for p in packages}

    def vuln(self, vuln_id: str) -> dict:
        return {
            "id": vuln_id,
            "database_specific": {"severity": "HIGH"},
            "affected": [
                {
                    "package": {"ecosystem": "Maven", "name": "com.squareup.okhttp3:okhttp"},
                    "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "5.0.0"}]}],
                }
            ],
        }

    def release_time(self, ecosystem: str, name: str, version: str) -> Any:
        from datetime import UTC, datetime, timedelta

        return datetime.now(UTC) - timedelta(days=2 if name.endswith(":okhttp") else 90)


def test_cooldown_fails_for_a_young_gradle_version_and_passes_when_exempt(
    gradle_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps_cli, "OsvClient", YoungClient)
    path = ["--path", str(gradle_project)]

    young = runner.invoke(app, ["deps", "cooldown", *path])
    assert young.exit_code == 1, young.output
    assert "INSIDE" in young.output and "com.squareup.okhttp3:okhttp 4.12.0" in young.output
    assert "okio" not in young.output

    exempt = runner.invoke(app, ["deps", "cooldown", *path, "--exempt", "com.squareup.okhttp3:okhttp"])
    assert exempt.exit_code == 0 and "exempt" in exempt.output

    shorter = runner.invoke(app, ["deps", "cooldown", *path, "--cooldown-days", "1"])
    assert shorter.exit_code == 0, shorter.output


def test_cooldown_exits_3_when_a_release_time_cannot_be_read(
    gradle_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Down(YoungClient):
        def release_time(self, ecosystem: str, name: str, version: str) -> Any:
            raise DataError("repository down")

    monkeypatch.setattr(deps_cli, "OsvClient", Down)

    assert runner.invoke(app, ["deps", "cooldown", "--path", str(gradle_project)]).exit_code == 3


def test_advisories_for_gradle_point_at_the_catalog_and_the_cooldown_exception(
    gradle_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps_cli, "OsvClient", YoungClient)

    result = runner.invoke(app, ["deps", "advisories", "--path", str(gradle_project)])

    assert result.exit_code == 1, result.output
    assert "[exempt-eligible] com.squareup.okhttp3:okhttp 4.12.0" in result.output and "-> 5.0.0" in result.output
    assert "gradle/libs.versions.toml" in result.output
    assert "bdt deps cooldown --exempt com.squareup.okhttp3:okhttp" in result.output


def test_cooldown_lists_catalog_entries_it_cannot_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "gradle").mkdir()
    (tmp_path / "gradle" / "libs.versions.toml").write_text(
        CATALOG + 'compose-ui = { module = "androidx.compose.ui:ui" }\n', encoding="utf-8"
    )
    monkeypatch.setattr(deps_cli, "OsvClient", YoungClient)

    result = runner.invoke(app, ["deps", "cooldown", "--path", str(tmp_path), "--cooldown-days", "1"])

    assert result.exit_code == 0, result.output
    assert "[unsupported] androidx.compose.ui:ui" in result.output and "none of the 2 checked" in result.output
