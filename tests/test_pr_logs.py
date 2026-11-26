import json
from dataclasses import replace

from bmsdna.devtools.pr_logs import (
    NO_LOG,
    LogSection,
    LogSource,
    Options,
    ado_sections,
    clean_log,
    github_source,
    matches_step,
    render,
    tail_lines,
    to_json,
)

TS = "2024-03-21T15:01:23.1234567Z "

TIMELINE = [
    {"id": "s1", "type": "Stage", "name": "Test", "parentId": None},
    {"id": "p1", "type": "Phase", "name": "Phase", "parentId": "s1"},
    {"id": "j1", "type": "Job", "name": "unit", "parentId": "p1"},
    {
        "id": "t2",
        "type": "Task",
        "name": "pytest",
        "parentId": "j1",
        "result": "failed",
        "startTime": "2024-03-21T15:02:00Z",
        "log": {"url": "https://ado/logs/2"},
    },
    {
        "id": "t1",
        "type": "Task",
        "name": "uv sync",
        "parentId": "j1",
        "result": "succeeded",
        "startTime": "2024-03-21T15:01:00Z",
        "log": {"url": "https://ado/logs/1"},
    },
    {"id": "t3", "type": "Task", "name": "skipped", "parentId": "j1", "result": "skipped", "log": None},
]


def fetch(url: str) -> str:
    return f"{TS}log of {url}"


def test_clean_log_strips_timestamps_unless_asked() -> None:
    assert clean_log(f"{TS}a\n{TS}b") == "a\nb"
    assert clean_log(f"{TS}a", timestamps=True) == f"{TS}a"


def test_tail_lines() -> None:
    assert tail_lines("a\nb\nc", 0) == "a\nb\nc"
    assert tail_lines("a\nb\nc", 3) == "a\nb\nc"
    out = tail_lines("a\nb\nc\nd", 2)
    assert out.splitlines()[1:] == ["c", "d"]
    assert "2 earlier lines omitted" in out.splitlines()[0]


def test_matches_step_is_case_insensitive_substring() -> None:
    assert matches_step("Test / unit / PyTest", "pytest")
    assert not matches_step("Test / unit / pytest", "lint")
    assert matches_step("anything", None)


def test_ado_sections_lists_every_task_with_a_log_chronologically() -> None:
    sections = ado_sections(TIMELINE, fetch, step=None)
    assert [(s.title, s.result) for s in sections] == [
        ("Test / unit / uv sync", "succeeded"),
        ("Test / unit / pytest", "failed"),
    ]
    assert sections[1].fetch() == f"{TS}log of https://ado/logs/2"


def test_ado_sections_step_filter() -> None:
    sections = ado_sections(TIMELINE, fetch, step="sync")
    assert [s.title for s in sections] == ["Test / unit / uv sync"]


def make_source() -> LogSource:
    ok = LogSection("unit / sync", "succeeded", lambda: f"{TS}installed")
    bad = LogSection("unit / pytest", "failed", lambda: f"{TS}boom\n{TS}again")
    return LogSource(7, "ci", "failed", "https://dev.azure.com/o/p/_build/results?buildId=7", [ok, bad])


OPTS = Options()


def test_render_default_is_one_line_for_succeeded_and_full_log_for_failed() -> None:
    out = render(make_source(), OPTS).splitlines()
    assert out[:3] == [
        "=== Build 7  ci  [failed] ===",
        "https://dev.azure.com/o/p/_build/results?buildId=7",
        "[succeeded] unit / sync",
    ]
    assert "installed" not in "\n".join(out)
    assert out[-3:] == ["--- [failed] unit / pytest ---", "boom", "again"]


def test_render_all_and_step_print_succeeded_logs_too() -> None:
    assert "installed" in render(make_source(), replace(OPTS, show_all=True))
    picked = render(make_source(), replace(OPTS, step="sync"))
    assert "installed" in picked and "boom" in picked  # the filter itself is applied by the sources


def test_render_list_only_never_fetches() -> None:
    def explode() -> str:
        raise AssertionError("fetched")

    source = LogSource(1, "ci", "failed", "", [LogSection("a", "failed", explode)])
    assert render(source, replace(OPTS, list_only=True)).splitlines() == ["=== Build 1  ci  [failed] ===", "[failed] a"]


def test_render_tail_and_empty_log() -> None:
    assert render(make_source(), replace(OPTS, tail=1)).endswith("again") and "boom" not in render(
        make_source(), replace(OPTS, tail=1)
    )
    blank = LogSource(1, "ci", "failed", "", [LogSection("a", "failed", lambda: "  \n")])
    assert NO_LOG in render(blank, OPTS)
    assert "(no steps with logs)" in render(LogSource(1, "ci", "failed", "", []), OPTS)


def test_to_json_has_log_only_for_failed_steps() -> None:
    doc = json.loads(to_json([make_source()], OPTS))
    assert doc[0]["id"] == 7 and doc[0]["name"] == "ci" and doc[0]["result"] == "failed"
    assert [(s["name"], s["result"], s["log"]) for s in doc[0]["steps"]] == [
        ("unit / sync", "succeeded", None),
        ("unit / pytest", "failed", "boom\nagain"),
    ]


def test_to_json_all_and_list() -> None:
    steps = json.loads(to_json([make_source()], replace(OPTS, show_all=True)))[0]["steps"]
    assert steps[0]["log"] == "installed"
    steps = json.loads(to_json([make_source()], replace(OPTS, list_only=True)))[0]["steps"]
    assert [s["log"] for s in steps] == [None, None]


class FakeGh:
    repo_path = "repos/o/r"

    def get(self, path: str, **kwargs: object) -> dict:
        if path.endswith("/jobs"):
            return {
                "jobs": [
                    {
                        "id": 1,
                        "name": "test",
                        "conclusion": "failure",
                        "steps": [
                            {"name": "pytest", "conclusion": "failure"},
                            {"name": "setup", "conclusion": "success"},
                        ],
                    },
                    {"id": 2, "name": "lint", "conclusion": "success", "steps": []},
                ]
            }
        return {"name": "CI", "conclusion": "failure", "html_url": "https://github.com/o/r/actions/runs/9"}


def test_github_source_lists_every_job_with_failed_steps_named() -> None:
    source = github_source(FakeGh(), 9, None, step=None)  # ty: ignore[invalid-argument-type]
    assert source.header == "Run 9  CI  [failure]"
    assert source.url.endswith("/actions/runs/9")
    assert [(s.title, s.failed) for s in source.sections] == [("test -- failed step(s): pytest", True), ("lint", False)]
