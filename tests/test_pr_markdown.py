from bmsdna.devtools.pr_markdown import (
    build_attachments_section,
    build_attachments_section_html,
    build_screenshots_section,
    build_screenshots_section_html,
)


def test_build_screenshots_section_appends_section_with_images() -> None:
    text = build_screenshots_section("existing text", [("before.png", "https://x/1"), ("after.png", "https://x/2")])
    assert text.startswith("existing text\n\n## Screenshots\n\n")
    assert "![before.png](https://x/1)" in text
    assert "![after.png](https://x/2)" in text


def test_build_screenshots_section_handles_no_existing_text() -> None:
    text = build_screenshots_section(None, [("shot.png", "https://x/1")])
    assert text == "\n\n## Screenshots\n\n![shot.png](https://x/1)\n"


def test_build_screenshots_section_html_uses_img_tags_not_markdown() -> None:
    text = build_screenshots_section_html("existing text", [("before.png", "https://x/1"), ("after.png", "https://x/2")])
    assert text.startswith("existing text\n\n<h2>Screenshots</h2>\n\n")
    assert '<img src="https://x/1" alt="before.png" style="max-width:100%;">' in text
    assert '<img src="https://x/2" alt="after.png" style="max-width:100%;">' in text
    assert "![" not in text


def test_build_screenshots_section_html_handles_no_existing_text() -> None:
    text = build_screenshots_section_html(None, [("shot.png", "https://x/1")])
    assert text == '\n\n<h2>Screenshots</h2>\n\n<img src="https://x/1" alt="shot.png" style="max-width:100%;">\n'


def test_build_screenshots_section_html_escapes_special_characters() -> None:
    text = build_screenshots_section_html(None, [('a "tricky" <name>.png', "https://x/1?a=1&b=2")])
    assert "&quot;tricky&quot;" in text
    assert "&lt;name&gt;" in text
    assert "&amp;b=2" in text
    assert "<name>" not in text


def test_build_attachments_section_appends_section_with_links() -> None:
    text = build_attachments_section("existing text", [("log.txt", "https://x/1"), ("report.pdf", "https://x/2")])
    assert text.startswith("existing text\n\n## Attachments\n\n")
    assert "- [log.txt](https://x/1)" in text
    assert "- [report.pdf](https://x/2)" in text


def test_build_attachments_section_handles_no_existing_text() -> None:
    text = build_attachments_section(None, [("log.txt", "https://x/1")])
    assert text == "\n\n## Attachments\n\n- [log.txt](https://x/1)\n"


def test_build_attachments_section_html_uses_anchor_tags_not_markdown() -> None:
    text = build_attachments_section_html("existing text", [("log.txt", "https://x/1"), ("report.pdf", "https://x/2")])
    assert text.startswith("existing text\n\n<h2>Attachments</h2>\n\n")
    assert '<a href="https://x/1">log.txt</a>' in text
    assert '<a href="https://x/2">report.pdf</a>' in text
    assert "[" not in text


def test_build_attachments_section_html_handles_no_existing_text() -> None:
    text = build_attachments_section_html(None, [("log.txt", "https://x/1")])
    assert text == '\n\n<h2>Attachments</h2>\n\n<a href="https://x/1">log.txt</a><br>\n'


def test_build_attachments_section_html_escapes_special_characters() -> None:
    text = build_attachments_section_html(None, [('a "tricky" <name>.txt', "https://x/1?a=1&b=2")])
    assert "&quot;tricky&quot;" in text
    assert "&lt;name&gt;" in text
    assert "&amp;b=2" in text
    assert "<name>" not in text
