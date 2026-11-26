import pytest

from bmsdna.devtools.pr_logs import (
    NO_LOG,
    LogSection,
    LogSource,
    ado_sections,
    clean_log,
    github_source,
    matches_step,
    render,
    tail_lines,
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


def test_ado_sections_default_is_failed_tasks_with_path() -> None:
    sections = ado_sections(TIMELINE, fetch, show_all=False, step=None)
    assert [(s.title, s.result) for s in sections] == [("Test / unit / pytest", "failed")]
    assert sections[0].fetch() == f"{TS}log of https://ado/logs/2"


def test_ado_sections_all_is_chronological_and_skips_tasks_without_log() -> None:
    sections = ado_sections(TIMELINE, fetch, show_all=True, step=None)
    assert [s.title for s in sections] == ["Test / unit / uv sync", "Test / unit / pytest"]


def test_ado_sections_step_filter() -> None:
    sections = ado_sections(TIMELINE, fetch, show_all=True, step="sync")
    assert [s.title for s in sections] == ["Test / unit / uv sync"]


def test_render_fetches_lazily_and_formats() -> None:
    calls: list[int] = []

    def log() -> str:
        calls.append(1)
        return f"{TS}boom\n{TS}again"

    source = LogSource("Build 7  ci  [failed]", [LogSection("unit / pytest", "failed", log)])
    listed = render(source, list_only=True, tail=0, timestamps=False, show_all=False)
    assert calls == []
    assert listed.splitlines() == ["=== Build 7  ci  [failed] ===", "--- [failed] unit / pytest ---"]
    shown = render(source, list_only=False, tail=1, timestamps=False, show_all=False)
    assert shown.splitlines()[0] == "=== Build 7  ci  [failed] ==="
    assert shown.endswith("again") and "boom" not in shown


def test_render_empty_log_and_empty_source() -> None:
    assert NO_LOG in render(
        LogSource("h", [LogSection("a", "failed", lambda: "  \n")]),
        list_only=False,
        tail=0,
        timestamps=False,
        show_all=True,
    )
    hint = render(LogSource("h", []), list_only=False, tail=0, timestamps=False, show_all=False)
    assert "no failed steps" in hint and "--all" in hint


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


@pytest.mark.parametrize(("show_all", "expected"), [(False, ["test -- failed step(s): pytest"]), (True, 2)])
def test_github_source_jobs(show_all: bool, expected: list[str] | int) -> None:
    source = github_source(FakeGh(), 9, None, show_all=show_all, step=None)  # ty: ignore[invalid-argument-type]
    assert "Run 9  CI  [failure]" in source.header
    if isinstance(expected, int):
        assert len(source.sections) == expected
    else:
        assert [s.title for s in source.sections] == expected
