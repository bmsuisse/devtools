from bmsdna.devtools.pr_markdown import build_screenshots_section


def test_build_screenshots_section_appends_section_with_images() -> None:
    text = build_screenshots_section("existing text", [("before.png", "https://x/1"), ("after.png", "https://x/2")])
    assert text.startswith("existing text\n\n## Screenshots\n\n")
    assert "![before.png](https://x/1)" in text
    assert "![after.png](https://x/2)" in text


def test_build_screenshots_section_handles_no_existing_text() -> None:
    text = build_screenshots_section(None, [("shot.png", "https://x/1")])
    assert text == "\n\n## Screenshots\n\n![shot.png](https://x/1)\n"
