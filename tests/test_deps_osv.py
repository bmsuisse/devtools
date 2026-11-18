from datetime import datetime
from typing import Any

import pytest
import requests

from bmsdna.devtools.deps import osv
from bmsdna.devtools.deps.osv import DataError, OsvClient


class FakeResponse:
    def __init__(self, payload: Any = None, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._payload, self.status_code, self.headers = payload, status, headers or {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        return self._payload


class FakeSession(requests.Session):
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[tuple[str | bytes, str | bytes, dict[str, Any]]] = []

    def request(self, method: str | bytes, url: str | bytes, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def head(self, url: str | bytes, *args: Any, **kwargs: Any) -> Any:
        return self.request("HEAD", url, **kwargs)


def _day(released: datetime | None) -> int:
    assert released is not None
    return released.day


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osv.time, "sleep", lambda _: None)


def test_query_batch_follows_next_page_tokens() -> None:
    session = FakeSession(
        [
            FakeResponse({"results": [{"vulns": [{"id": "A"}], "next_page_token": "t1"}, {}]}),
            FakeResponse({"results": [{"vulns": [{"id": "B"}]}]}),
        ]
    )
    result = OsvClient(session).query_batch([("PyPI", "x", "1.0"), ("npm", "y", "2.0.0"), ("PyPI", "x", "1.0")])

    assert result == {("PyPI", "x", "1.0"): ["A", "B"], ("npm", "y", "2.0.0"): []}
    assert session.calls[1][2]["json"]["queries"] == [
        {"package": {"ecosystem": "PyPI", "name": "x"}, "version": "1.0", "page_token": "t1"}
    ]


def test_a_short_batch_answer_is_an_error_not_a_clean_result() -> None:
    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse({"results": []})])).query_batch([("PyPI", "x", "1.0")])


def test_transient_failures_are_retried_then_raised() -> None:
    session = FakeSession([FakeResponse(status=503), requests.ConnectionError("boom"), FakeResponse({"id": "A"})])
    assert OsvClient(session).vuln("A") == {"id": "A"}

    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse(status=503)] * 3)).vuln("A")
    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse({"id": "other"})])).vuln("A")


def test_kev_is_read_by_cve_id_and_a_bad_feed_is_an_error() -> None:
    assert OsvClient(FakeSession([FakeResponse({"vulnerabilities": [{"cveID": "cve-2026-1"}]})])).kev_cves() == {
        "CVE-2026-1"
    }
    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse({"unexpected": []})])).kev_cves()


def test_release_time_uses_the_youngest_pypi_file_and_npm_time_map_and_none_for_private_packages() -> None:
    pypi = FakeResponse(
        {"urls": [{"upload_time_iso_8601": "2026-11-01T00:00:00.1Z"}, {"upload_time_iso_8601": "2026-11-03T00:00:00Z"}]}
    )
    assert _day(OsvClient(FakeSession([pypi])).release_time("PyPI", "x", "1.0")) == 3
    npm = FakeResponse({"time": {"1.0.0": "2026-11-05T01:00:00.000Z"}})
    assert _day(OsvClient(FakeSession([npm])).release_time("npm", "@s/p", "1.0.0")) == 5
    assert OsvClient(FakeSession([FakeResponse(status=404)])).release_time("PyPI", "internal-only", "1.0") is None


def test_a_repeating_page_token_is_an_error_not_an_endless_loop() -> None:
    page = {"results": [{"vulns": [{"id": "A"}], "next_page_token": "same"}]}
    session = FakeSession([FakeResponse(page), FakeResponse(page), FakeResponse(page)])

    with pytest.raises(DataError, match="does not terminate"):
        OsvClient(session).query_batch([("PyPI", "x", "1.0")])


def test_maven_release_time_is_the_pom_last_modified_of_the_first_repository_that_has_it() -> None:
    session = FakeSession(
        [FakeResponse(status=404), FakeResponse(headers={"Last-Modified": "Wed, 13 Aug 2025 20:29:43 GMT"})]
    )
    released = OsvClient(session).release_time("Maven", "androidx.core:core-ktx", "1.17.0")

    assert released is not None and (released.year, released.month, released.day, released.hour) == (2025, 8, 13, 20)
    assert [call[1] for call in session.calls] == [
        "https://repo1.maven.org/maven2/androidx/core/core-ktx/1.17.0/core-ktx-1.17.0.pom",
        "https://dl.google.com/dl/android/maven2/androidx/core/core-ktx/1.17.0/core-ktx-1.17.0.pom",
    ]


def test_maven_release_time_is_none_when_no_repository_has_it_and_an_error_when_one_is_down() -> None:
    assert OsvClient(FakeSession([FakeResponse(status=404)] * 3)).release_time("Maven", "internal:lib", "1") is None
    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse(status=503)] * 3)).release_time("Maven", "a:b", "1")
    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse(headers={"Last-Modified": "garbage"})])).release_time("Maven", "a:b", "1")


def test_a_maven_pom_without_last_modified_is_an_error_not_an_unknown_age() -> None:
    with pytest.raises(DataError, match="Last-Modified"):
        OsvClient(FakeSession([FakeResponse(headers={})])).release_time("Maven", "a:b", "1")


def test_crates_io_release_time_is_created_at_and_sends_a_user_agent() -> None:
    session = FakeSession([FakeResponse({"version": {"num": "1.0.200", "created_at": "2024-05-01T16:00:47.131059Z"}})])

    released = OsvClient(session).release_time("crates.io", "serde", "1.0.200")

    assert _day(released) == 1
    assert session.calls[0][1] == "https://crates.io/api/v1/crates/serde/1.0.200"
    assert "bmsuisse" in session.calls[0][2]["headers"]["User-Agent"]


def test_crates_io_unknown_version_is_none_and_a_record_without_created_at_is_an_error() -> None:
    assert OsvClient(FakeSession([FakeResponse(status=404)])).release_time("crates.io", "private", "1.0.0") is None
    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse({"version": {}})])).release_time("crates.io", "serde", "1.0.200")


def test_nuget_release_time_is_published_of_the_lower_cased_registration_leaf() -> None:
    session = FakeSession([FakeResponse({"published": "2023-03-08T07:46:17.78+00:00"})])

    released = OsvClient(session).release_time("NuGet", "Newtonsoft.Json", "13.0.3")

    assert _day(released) == 8
    assert session.calls[0][1] == "https://api.nuget.org/v3/registration5-gz-semver2/newtonsoft.json/13.0.3.json"


def test_nuget_unlisted_package_date_1900_is_unknown_not_ancient() -> None:
    unlisted = FakeResponse({"published": "1900-01-01T00:00:00+00:00"})
    assert OsvClient(FakeSession([unlisted])).release_time("NuGet", "X", "1.0.0") is None
    assert OsvClient(FakeSession([FakeResponse(status=404)])).release_time("NuGet", "X", "1.0.0") is None
