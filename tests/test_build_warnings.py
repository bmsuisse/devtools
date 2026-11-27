import json
from unittest.mock import MagicMock

import requests

from bmsdna.devtools import gh_pr, pr_build, pr_logs
from bmsdna.devtools.build_warnings import (
    BuildWarning,
    ado_warning_count,
    ado_warnings,
    count_text,
    github_warnings,
    log_warnings,
)
from bmsdna.devtools.gitrepo import AdoRemote
from bmsdna.devtools.pr_warnings import WarningSource, ado_source, render, to_json

TS = "2024-03-21T15:01:23.1234567Z "


def warning(message: str, **data: str) -> dict:
    return {"type": "warning", "message": message, "data": data}


RECORDS = [
    {"id": "s", "type": "Stage", "name": "Test", "parentId": None},
    {"id": "j", "type": "Job", "name": "unit", "parentId": "s", "warningCount": 3, "issues": []},
    {
        "id": "t1",
        "type": "Task",
        "name": "uv sync",
        "parentId": "j",
        "warningCount": 2,
        "issues": [warning("old pin", sourcepath="pyproject.toml", linenumber="3"), {"type": "error", "message": "x"}],
        "log": {"url": "https://ado/logs/1"},
    },
    {"id": "t2", "type": "Task", "name": "pytest", "parentId": "j", "warningCount": 1, "issues": [warning("slow")]},
    {"id": "t3", "type": "Task", "name": "lint", "parentId": "j", "result": "succeeded"},
]


def path(rec: dict) -> str:
    return pr_logs.record_path({r["id"]: r for r in RECORDS}, rec)


def test_count_text_pluralises_and_names_the_command() -> None:
    assert count_text(12, 5) == "12 Warnings (see with bdt pr warnings --build-id 5)"
    assert count_text(1, 5).startswith("1 Warning (")


def test_ado_warning_count_uses_task_counts_not_the_aggregating_job_count() -> None:
    assert ado_warning_count(RECORDS) == 3
    assert ado_warning_count([]) == 0


def test_log_warnings_reads_the_warning_lines() -> None:
    assert log_warnings(f"{TS}##[warning]a\n{TS}##[error]b\n{TS}##[warning] c ") == ["a", "c"]


def test_ado_warnings_prefers_the_log_when_issues_are_capped() -> None:
    log = f"{TS}##[warning]one\n{TS}##[warning]two\n"
    found = ado_warnings(RECORDS, path, lambda url: log)
    assert found == [
        BuildWarning("Test / unit / uv sync", "one"),
        BuildWarning("Test / unit / uv sync", "two"),
        BuildWarning("Test / unit / pytest", "slow"),
    ]


def test_ado_warnings_keeps_the_issues_when_the_log_cannot_be_read() -> None:
    def broken(url: str) -> str:
        raise requests.ConnectionError

    found = ado_warnings(RECORDS, path, broken)
    assert [w.message for w in found] == ["pyproject.toml:3: old pin", "slow"]


class FakeGh:
    repo_path = "repos/o/r"

    def paginate(self, path: str, params: dict | None = None, *, items_key: str | None = None) -> list[dict]:
        if path.endswith("/jobs"):
            return [{"id": 1, "name": "test"}, {"id": 2, "name": "skipped one", "conclusion": "skipped"}]
        assert path == "repos/o/r/check-runs/1/annotations"
        return [
            {
                "annotation_level": "warning",
                "path": ".github/ci.yml",
                "start_line": 4,
                "title": "Node",
                "message": "old",
            },
            {"annotation_level": "notice", "message": "fyi"},
            {"annotation_level": "failure", "message": "exit 1"},
        ]


def test_github_warnings_are_the_warning_annotations_of_non_skipped_jobs() -> None:
    assert github_warnings(FakeGh(), 9) == [BuildWarning("test", ".github/ci.yml:4: Node: old")]  # ty: ignore[invalid-argument-type]


def test_render_groups_by_step_and_notes_unlisted_warnings() -> None:
    src = WarningSource(
        7, "ci", "succeeded", "https://x/_build?buildId=7", 3, [BuildWarning("a", "m1"), BuildWarning("a", "m2")]
    )
    out = render(src).splitlines()
    assert out[0] == "=== Build 7  ci  [succeeded] ==="
    assert "3 Warnings" in out and "--- a ---" in out and out[-2:] == ["m1", "m2"]
    assert "only 2 of them could be listed" in render(src)
    assert render(WarningSource(7, "ci", "succeeded", "", 0, [])).endswith("No warnings.")


def test_to_json() -> None:
    doc = json.loads(to_json([WarningSource(7, "ci", "failed", "u", 1, [BuildWarning("a", "m")])]))
    assert doc == [
        {"id": 7, "name": "ci", "result": "failed", "url": "u", "count": 1, "warnings": [{"step": "a", "message": "m"}]}
    ]


def test_ado_source_counts_what_the_timeline_reports(monkeypatch) -> None:
    monkeypatch.setattr(pr_build, "get_timeline_records", lambda *a: RECORDS)
    session = MagicMock()
    session.get.return_value.text = f"{TS}##[warning]one\n##[warning]two"
    build = {"id": 7, "result": "succeeded", "definition": {"name": "ci"}}
    source = ado_source(session, AdoRemote("o", "p", "r"), build)
    assert source.count == 3 and len(source.warnings) == 3 and source.name == "ci"


def test_print_build_shows_the_warning_count(monkeypatch, capsys) -> None:
    monkeypatch.setattr(pr_build, "get_timeline_records", lambda *a: RECORDS)
    build = {
        "id": 7,
        "status": "completed",
        "result": "succeeded",
        "definition": {"name": "ci"},
        "sourceVersion": "abcdef123",
    }
    pr_build.print_build(None, AdoRemote("o", "p", "r"), build)  # ty: ignore[invalid-argument-type]
    assert "  3 Warnings (see with bdt pr warnings --build-id 7)" in capsys.readouterr().out


def test_print_build_without_warnings_or_timeline_stays_quiet(monkeypatch, capsys) -> None:
    def no_timeline(*a: object) -> list:
        raise requests.ConnectionError

    monkeypatch.setattr(pr_build, "get_timeline_records", no_timeline)
    build = {"id": 7, "status": "completed", "result": "succeeded", "definition": {}, "sourceVersion": "abcdef123"}
    pr_build.print_build(None, AdoRemote("o", "p", "r"), build)  # ty: ignore[invalid-argument-type]
    assert "Warning" not in capsys.readouterr().out


def test_github_status_prints_warning_counts_for_finished_runs(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "bmsdna.devtools.build_warnings.github_warnings", lambda gh, run_id: [BuildWarning("t", "w")] * (run_id - 8)
    )
    done = {
        "__typename": "CheckRun",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "name": "a",
        "workflowName": "CI",
        "detailsUrl": "https://github.com/o/r/actions/runs/{}/job/1",
    }
    runs = [{**done, "detailsUrl": done["detailsUrl"].format(n)} for n in (8, 10)]
    gh_pr.print_warning_counts(None, runs)  # ty: ignore[invalid-argument-type]
    out = capsys.readouterr().out
    assert "Run #10" in out and "2 Warnings (see with bdt pr warnings --build-id 10)" in out and "Run #8" not in out
