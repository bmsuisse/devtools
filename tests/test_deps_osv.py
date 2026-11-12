from typing import Any

import pytest
import requests

from bmsdna.devtools.deps import osv
from bmsdna.devtools.deps.osv import DataError, OsvClient


class FakeResponse:
    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self._payload, self.status_code = payload, status

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses, self.calls = responses, []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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
    result = OsvClient(session).query_batch([("PyPI", "x", "1.0"), ("npm", "y", "2.0.0"), ("PyPI", "x", "1.0")])  # type: ignore[arg-type]

    assert result == {("PyPI", "x", "1.0"): ["A", "B"], ("npm", "y", "2.0.0"): []}
    assert session.calls[1][2]["json"]["queries"] == [
        {"package": {"ecosystem": "PyPI", "name": "x"}, "version": "1.0", "page_token": "t1"}
    ]


def test_a_short_batch_answer_is_an_error_not_a_clean_result() -> None:
    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse({"results": []})])).query_batch([("PyPI", "x", "1.0")])  # type: ignore[arg-type]


def test_transient_failures_are_retried_then_raised() -> None:
    session = FakeSession([FakeResponse(status=503), requests.ConnectionError("boom"), FakeResponse({"id": "A"})])
    assert OsvClient(session).vuln("A") == {"id": "A"}  # type: ignore[arg-type]

    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse(status=503)] * 3)).vuln("A")  # type: ignore[arg-type]
    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse({"id": "other"})])).vuln("A")  # type: ignore[arg-type]


def test_kev_is_read_by_cve_id_and_a_bad_feed_is_an_error() -> None:
    assert OsvClient(FakeSession([FakeResponse({"vulnerabilities": [{"cveID": "cve-2026-1"}]})])).kev_cves() == {
        "CVE-2026-1"
    }  # type: ignore[arg-type]
    with pytest.raises(DataError):
        OsvClient(FakeSession([FakeResponse({"unexpected": []})])).kev_cves()  # type: ignore[arg-type]


def test_release_time_uses_the_youngest_pypi_file_and_npm_time_map_and_none_for_private_packages() -> None:
    pypi = FakeResponse(
        {"urls": [{"upload_time_iso_8601": "2026-11-01T00:00:00.1Z"}, {"upload_time_iso_8601": "2026-11-03T00:00:00Z"}]}
    )
    assert OsvClient(FakeSession([pypi])).release_time("PyPI", "x", "1.0").day == 3  # type: ignore[arg-type, union-attr]
    npm = FakeResponse({"time": {"1.0.0": "2026-11-05T01:00:00.000Z"}})
    assert OsvClient(FakeSession([npm])).release_time("npm", "@s/p", "1.0.0").day == 5  # type: ignore[arg-type, union-attr]
    assert OsvClient(FakeSession([FakeResponse(status=404)])).release_time("PyPI", "internal-only", "1.0") is None  # type: ignore[arg-type]
