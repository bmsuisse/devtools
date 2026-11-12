"""The network side of `bdt deps advisories`: OSV (advisories), CISA KEV (exploited CVEs) and the registries (release
times). Every failure that makes an answer incomplete raises `DataError`; callers must not turn it into "nothing found".
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from urllib.parse import quote

import requests

from ..cli_tools import HTTP_TIMEOUT_SECS
from .versions import Ecosystem

OSV_URL = "https://api.osv.dev"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
PYPI_URL = "https://pypi.org/pypi"
NPM_URL = "https://registry.npmjs.org"
BATCH_SIZE = 500
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECS = 1.0

PackageQuery = tuple[Ecosystem, str, str]  # ecosystem, name, version


class DataError(RuntimeError):
    """OSV, KEV or a registry could not be read completely."""


class AdvisorySource(Protocol):
    """What `advisories.scan` needs; `OsvClient` is the real one, tests pass a fake."""

    def query_batch(self, packages: Sequence[PackageQuery]) -> dict[PackageQuery, list[str]]: ...
    def vuln(self, vuln_id: str) -> dict[str, Any]: ...
    def kev_cves(self) -> set[str]: ...
    def release_time(self, ecosystem: Ecosystem, name: str, version: str) -> datetime | None: ...


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _parse_time(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class OsvClient:
    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._vulns: dict[str, dict[str, Any]] = {}
        self._npm_times: dict[str, dict[str, str]] = {}

    def _request(self, method: str, url: str, *, allow_404: bool = False, **kwargs: Any) -> Any:
        last: Exception | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = self._session.request(method, url, timeout=HTTP_TIMEOUT_SECS, **kwargs)
            except requests.RequestException as exc:
                last = exc
            else:
                if response.status_code == 404 and allow_404:
                    return None
                if response.status_code in (429, 500, 502, 503, 504):
                    last = DataError(f"HTTP {response.status_code} from {url}")
                elif not response.ok:
                    raise DataError(f"HTTP {response.status_code} from {url}")
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise DataError(f"{url} returned invalid JSON") from exc
            time.sleep(RETRY_BACKOFF_SECS * (attempt + 1))
        raise DataError(f"{url} unreachable: {last}")

    def query_batch(self, packages: Sequence[PackageQuery]) -> dict[PackageQuery, list[str]]:
        """Advisory ids per (ecosystem, name, version); follows every `next_page_token`."""
        unique = list(dict.fromkeys(packages))
        base = {
            i: {"package": {"ecosystem": eco, "name": name}, "version": version}
            for i, (eco, name, version) in enumerate(unique)
        }
        found: dict[int, list[str]] = {i: [] for i in base}
        pending = dict(base)
        while pending:
            next_pending: dict[int, dict[str, Any]] = {}
            for chunk in _chunks(list(pending), BATCH_SIZE):
                body = {"queries": [pending[i] for i in chunk]}
                payload: dict[str, Any] = self._request("POST", f"{OSV_URL}/v1/querybatch", json=body) or {}
                results = cast("list[dict[str, Any]]", payload.get("results"))
                if not isinstance(results, list) or len(results) != len(chunk):
                    raise DataError("OSV querybatch returned a different number of results than queries")
                for i, result in zip(chunk, results, strict=True):
                    found[i].extend(v["id"] for v in result.get("vulns", []))
                    if result.get("next_page_token"):
                        next_pending[i] = {
                            **base[i],
                            "page_token": result["next_page_token"],
                        }
            pending = next_pending
        return {unique[i]: sorted(dict.fromkeys(ids)) for i, ids in found.items()}

    def vuln(self, vuln_id: str) -> dict[str, Any]:
        if vuln_id not in self._vulns:
            record = self._request("GET", f"{OSV_URL}/v1/vulns/{quote(vuln_id, safe='')}")
            if not isinstance(record, dict) or record.get("id") != vuln_id:
                raise DataError(f"OSV returned a malformed record for {vuln_id}")
            self._vulns[vuln_id] = record
        return self._vulns[vuln_id]

    def kev_cves(self) -> set[str]:
        feed = self._request("GET", KEV_URL)
        try:
            return {entry["cveID"].upper() for entry in feed["vulnerabilities"]}
        except (KeyError, TypeError) as exc:
            raise DataError("CISA KEV feed has an unexpected shape") from exc

    def release_time(self, ecosystem: Ecosystem, name: str, version: str) -> datetime | None:
        """When the version was published: the youngest file for PyPI (uv gates per file), `time[version]` for npm
        (bun's source). None when the registry doesn't know it (private package)."""
        if ecosystem == "PyPI":
            data = self._request("GET", f"{PYPI_URL}/{quote(name)}/{quote(version)}/json", allow_404=True)
            files = (data or {}).get("urls") or []
            times = [f["upload_time_iso_8601"] for f in files if f.get("upload_time_iso_8601")]
            return max(_parse_time(t) for t in times) if times else None
        if name not in self._npm_times:
            data = self._request("GET", f"{NPM_URL}/{quote(name, safe='@')}", allow_404=True)
            self._npm_times[name] = (data or {}).get("time") or {}
        stamp = self._npm_times[name].get(version)
        return _parse_time(stamp) if stamp else None
