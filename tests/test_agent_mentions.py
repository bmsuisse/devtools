import pytest

from bmsdna.devtools.agent_mentions import detect_agent, last_comment_filter, mentions_agent, normalize_agent


@pytest.mark.parametrize(
    "text",
    [
        "@Claude please take a look",
        "@claude",
        "@CLAUDE, ok?",
        "hey @Claude.",
        "(@claude)",
        "line one\n@Claude can you fix this",
        "@Agent anyone free?",
        "@agent",
        '<a href="#">@Claude</a> please',
    ],
)
def test_mentions_claude(text: str) -> None:
    assert mentions_agent(text, "claude")


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "no mention here",
        "@Claudette please look",
        "@claude-bot please look",
        "@Claude2",
        "@Agents of shield",
        "mail me at someone@claude.ai",
        "Claude without the at sign",
        "@Codex only",
    ],
)
def test_does_not_mention_claude(text: str | None) -> None:
    assert not mentions_agent(text, "claude")


def test_each_agent_matches_its_own_handle_and_agent() -> None:
    assert mentions_agent("@Codex fix it", "codex")
    assert mentions_agent("@Goose fix it", "goose")
    assert not mentions_agent("@Goose fix it", "codex")
    assert mentions_agent("@Agent fix it", "goose")


def test_agent_name_is_normalized() -> None:
    assert mentions_agent("@claude hi", "Claude")
    assert mentions_agent("@claude hi", "@Claude")
    assert mentions_agent("@codex hi", "Codex CLI")


def test_without_a_detected_agent_only_agent_matches() -> None:
    assert mentions_agent("@Agent hi", None)
    assert not mentions_agent("@Claude hi", None)


def test_last_comment_filter() -> None:
    accept = last_comment_filter("claude")
    assert accept("@Claude go")
    assert not accept("thanks")
    assert not accept(None)


@pytest.mark.parametrize(
    "raw,expected",
    [("Claude", "claude"), ("claude-code_2-1-270_agent", "claude"), ("@Goose", "goose"), ("Codex CLI", "codex"), ("OpenCode", "opencode")],
)
def test_normalize_agent(raw: str, expected: str) -> None:
    assert normalize_agent(raw) == expected


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, None),
        ({"CLAUDECODE": "1"}, "claude"),
        ({"CLAUDE_CODE_SESSION_ID": "abc"}, "claude"),
        ({"CODEX_SANDBOX_NETWORK_DISABLED": "1"}, "codex"),
        ({"CODEX_THREAD_ID": "t"}, "codex"),
        ({"GOOSE_TERMINAL": "1"}, "goose"),
        ({"GOOSE_SESSION": "s"}, "goose"),
        ({"AI_AGENT": "claude-code_2-1-270_agent"}, "claude"),
        ({"AI_AGENT": "codex"}, "codex"),
        ({"GEMINI_CLI": "1"}, "gemini"),
        ({"CODEX_SANDBOX": ""}, None),
        ({"BDT_AGENT": "goose", "CLAUDECODE": "1"}, "goose"),
    ],
)
def test_detect_agent(env: dict[str, str], expected: str | None) -> None:
    assert detect_agent(env) == expected


def test_explicit_override_wins_over_everything() -> None:
    assert detect_agent({"BDT_AGENT": "goose", "CLAUDECODE": "1"}, override="Codex") == "codex"


def test_blank_override_is_ignored() -> None:
    assert detect_agent({"CLAUDECODE": "1"}, override="  ") == "claude"
