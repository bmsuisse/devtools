from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bmsdna.devtools.deps.advisories import fix_for, group_vulns, parse_vuln, scan
from bmsdna.devtools.deps.lockfiles import LockedPackage, LockfileContent
from bmsdna.devtools.deps.osv import DataError, PackageQuery
from bmsdna.devtools.deps.versions import Ecosystem

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
LOCK = Path("/repo/uv.lock")


def vuln(
    vuln_id: str,
    *,
    name: str = "pkg",
    ecosystem: str = "PyPI",
    events: list[dict] | None = None,
    severity: str | None = "HIGH",
    aliases: list[str] | None = None,
    versions: list[str] | None = None,
) -> dict[str, Any]:
    affected: dict[str, Any] = {"package": {"ecosystem": ecosystem, "name": name}}
    if events is not None:
        affected["ranges"] = [{"type": "ECOSYSTEM" if ecosystem == "PyPI" else "SEMVER", "events": events}]
    if versions is not None:
        affected["versions"] = versions
    record: dict[str, Any] = {
        "id": vuln_id,
        "aliases": aliases or [],
        "summary": f"summary of {vuln_id}",
        "affected": [affected],
    }
    if severity:
        record["database_specific"] = {"severity": severity}
    return record


class FakeSource:
    def __init__(
        self,
        ids: dict[PackageQuery, list[str]],
        records: list[dict],
        *,
        kev: set[str] | None = None,
        kev_down: bool = False,
        released: dict[tuple[str, str], datetime] | None = None,
        fail_vuln: str | None = None,
    ) -> None:
        self.ids, self.records = ids, {r["id"]: r for r in records}
        self.kev, self.kev_down, self.released, self.fail_vuln = kev or set(), kev_down, released or {}, fail_vuln

    def query_batch(self, packages):  # noqa: ANN001, ANN201
        return {p: self.ids.get(p, []) for p in packages}

    def vuln(self, vuln_id: str) -> dict[str, Any]:
        if vuln_id == self.fail_vuln:
            raise DataError("HTTP 503")
        return self.records[vuln_id]

    def kev_cves(self) -> set[str]:
        if self.kev_down:
            raise DataError("KEV down")
        return self.kev

    def release_time(self, ecosystem: Ecosystem, name: str, version: str) -> datetime | None:
        return self.released.get((name, version))


def lock(
    name: str = "pkg",
    version: str = "1.0.0",
    *,
    ecosystem: Ecosystem = "PyPI",
    direct: bool = False,
    declared: str | None = None,
) -> LockfileContent:
    content = LockfileContent(LOCK, ecosystem)
    content.packages.append(LockedPackage(ecosystem, name, version, direct, LOCK, declared))
    return content


def run(content: LockfileContent, records: list[dict], **kwargs: Any):  # noqa: ANN201
    pkg = content.packages[0]
    ids = {(pkg.ecosystem, pkg.name, pkg.version): [r["id"] for r in records]}
    return scan([content], FakeSource(ids, records, **kwargs), now=NOW)


FIX_2 = [{"introduced": "0"}, {"fixed": "2.0.0"}]


def test_high_severity_with_a_fresh_fix_is_exempt_eligible_and_names_the_exact_version() -> None:
    result = run(
        lock(direct=True, declared=">=1"),
        [vuln("GHSA-1", events=FIX_2)],
        released={("pkg", "2.0.0"): NOW - timedelta(days=1)},
    )

    (finding,) = result.findings
    assert (finding.status, finding.target, finding.fix_age_days) == ("exempt-eligible", "2.0.0", 1.0)
    assert "--exclude-newer-package pkg=false" in finding.commands[0] and "uv lock --check" in finding.commands[1]
    assert not result.incomplete


def test_a_fix_older_than_the_cooldown_is_a_plain_bump() -> None:
    (finding,) = run(
        lock(), [vuln("GHSA-1", events=FIX_2)], released={("pkg", "2.0.0"): NOW - timedelta(days=30)}
    ).findings
    assert finding.status == "bump"
    assert "--exclude-newer-package" not in finding.commands[0]


def test_low_severity_with_a_fresh_fix_waits() -> None:
    (finding,) = run(
        lock(), [vuln("GHSA-1", events=FIX_2, severity="LOW")], released={("pkg", "2.0.0"): NOW - timedelta(days=1)}
    ).findings
    assert (finding.status, finding.commands) == ("informational", [])


def test_kev_makes_a_low_severity_advisory_urgent() -> None:
    record = vuln("GHSA-1", events=FIX_2, severity="LOW", aliases=["CVE-2026-1"])
    (finding,) = run(
        lock(), [record], kev={"CVE-2026-1"}, released={("pkg", "2.0.0"): NOW - timedelta(days=1)}
    ).findings
    assert finding.status == "exempt-eligible" and finding.groups[0].kev


def test_missing_severity_never_gets_an_automatic_exception() -> None:
    (finding,) = run(
        lock(), [vuln("PYSEC-1", events=FIX_2, severity=None)], released={("pkg", "2.0.0"): NOW - timedelta(days=1)}
    ).findings
    assert finding.status == "needs-triage"


def test_unknown_fix_age_still_allows_the_exception_but_says_so() -> None:
    (finding,) = run(lock(), [vuln("GHSA-1", events=FIX_2)]).findings
    assert finding.status == "exempt-eligible" and finding.fix_age_days is None and "age unknown" in finding.reason


def test_ghsa_pysec_and_cve_are_one_vulnerability_and_the_target_fixes_all_urgent_ones() -> None:
    records = [
        vuln("GHSA-1", events=[{"introduced": "0"}, {"fixed": "1.5.0"}], aliases=["CVE-1", "PYSEC-1"]),
        vuln("PYSEC-1", events=[{"introduced": "0"}, {"fixed": "1.5.0"}], severity=None, aliases=["CVE-1", "GHSA-1"]),
        vuln("GHSA-2", events=[{"introduced": "0"}, {"fixed": "2.0.0"}], severity="CRITICAL"),
    ]
    assert len(group_vulns(parse_vuln(r) for r in records)) == 2

    (finding,) = run(lock(), records, released={("pkg", "2.0.0"): NOW - timedelta(days=1)}).findings
    assert finding.target == "2.0.0" and len(finding.groups) == 2


def test_no_fixed_version_is_reported_not_skipped() -> None:
    (finding,) = run(lock(), [vuln("GHSA-1", events=[{"introduced": "0"}])]).findings
    assert finding.status == "no-fix"


def test_a_fix_the_declared_range_excludes_is_blocked() -> None:
    (finding,) = run(lock(direct=True, declared="==1.0.0"), [vuln("GHSA-1", events=FIX_2)]).findings
    assert finding.status == "blocked" and "widen" in finding.reason


def test_malicious_release_is_an_incident_not_a_bump() -> None:
    record = vuln("MAL-2026-1", events=[{"introduced": "0"}], severity=None)
    (finding,) = run(lock(), [record]).findings
    assert finding.status == "malicious" and finding.commands == []


def test_unparseable_or_unrelated_range_goes_to_triage() -> None:
    (finding,) = run(
        lock(version="1.0.0"), [vuln("GHSA-1", events=[{"introduced": "5.0.0"}, {"fixed": "6.0.0"}])]
    ).findings
    assert finding.status == "needs-triage"


def test_fail_closed_on_unreadable_advisory_and_unavailable_kev() -> None:
    records = [vuln("GHSA-1", events=FIX_2), vuln("GHSA-2", events=FIX_2)]
    result = run(lock(), records, fail_vuln="GHSA-2")
    assert any("GHSA-2" in reason for reason in result.incomplete)

    result = run(lock(), records[:1], kev_down=True)
    assert any("KEV" in reason for reason in result.incomplete)
    assert result.findings  # what could be evaluated is still reported


def test_a_clean_lock_has_no_findings_and_counts_what_it_scanned() -> None:
    result = run(lock(), [])
    assert (result.findings, result.incomplete, result.scanned) == ([], [], 1)


def test_npm_semver_ranges_and_zero_introduced() -> None:
    record = vuln("GHSA-n", name="lodash", ecosystem="npm", events=[{"introduced": "0"}, {"fixed": "4.17.21"}])
    state, fixed, _ = fix_for(parse_vuln(record), "npm", "lodash", "4.17.15")
    assert (state, fixed) == ("fixed", "4.17.21")
    assert fix_for(parse_vuln(record), "npm", "lodash", "4.17.21")[0] == "unknown"  # not inside any affected range


def test_multiple_intervals_pick_the_interval_that_contains_the_locked_version() -> None:
    events = [{"introduced": "0"}, {"fixed": "1.2.0"}, {"introduced": "2.0.0"}, {"fixed": "2.1.0"}]
    parsed = parse_vuln(vuln("GHSA-1", events=events))
    assert fix_for(parsed, "PyPI", "pkg", "1.1.0")[1] == "1.2.0"
    assert fix_for(parsed, "PyPI", "pkg", "2.0.5")[1] == "2.1.0"


def test_cvss3_scores_match_the_published_values() -> None:
    from bmsdna.devtools.deps.advisories import cvss3_score

    assert cvss3_score("CVSS:3.1/AV:N/AC:L/PR:H/UI:N/S:U/C:H/I:H/A:H") == 7.2  # the lodash value bun audit reports
    assert cvss3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H") == 10.0
    assert cvss3_score("CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:N/A:N") == 6.1
    assert cvss3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N") == 0.0
    assert (
        cvss3_score("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N") is None
    )  # v4 is not interpreted
    assert cvss3_score("garbage") is None


def test_cvss_score_alone_makes_an_advisory_urgent_and_a_disagreeing_label_goes_to_triage() -> None:
    high = {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:H/UI:N/S:U/C:H/I:H/A:H"}  # 7.2
    fresh = {("pkg", "2.0.0"): NOW - timedelta(days=1)}

    no_label = {**vuln("PYSEC-1", events=FIX_2, severity=None), "severity": [high]}
    assert run(lock(), [no_label], released=fresh).findings[0].status == "exempt-eligible"

    disagree = {**vuln("GHSA-1", events=FIX_2, severity="LOW"), "severity": [high]}
    (finding,) = run(lock(), [disagree], released=fresh).findings
    assert finding.status == "needs-triage" and finding.groups[0].severity == "conflicting"


def test_lower_severity_advisories_stay_open_at_the_urgent_target_and_the_finding_says_so() -> None:
    records = [
        vuln("GHSA-1", events=FIX_2, severity="CRITICAL"),
        vuln("GHSA-2", events=[{"introduced": "0"}, {"fixed": "3.0.0"}], severity="LOW"),
    ]
    (finding,) = run(lock(), records, released={("pkg", "2.0.0"): NOW - timedelta(days=1)}).findings

    assert (finding.status, finding.target) == ("exempt-eligible", "2.0.0")
    assert "GHSA-2 (fixed in 3.0.0)" in finding.reason
