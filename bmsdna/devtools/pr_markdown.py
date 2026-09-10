"""Markdown building shared by the Azure DevOps and GitHub screenshot-attach paths."""

from __future__ import annotations

from html import escape


def build_screenshots_section(existing_text: str | None, images: list[tuple[str, str]]) -> str:
    """Append a '## Screenshots' markdown section of `images` (name, url) to `existing_text`.

    For resources that are reliably Markdown-rendered (GitHub issues/PRs; Azure DevOps Git PR
    descriptions/comments) -- see `build_screenshots_section_html` for one that isn't.
    """
    section = "\n".join(f"![{name}]({url})" for name, url in images)
    return f"{existing_text or ''}\n\n## Screenshots\n\n{section}\n"


def build_screenshots_section_html(existing_text: str | None, images: list[tuple[str, str]]) -> str:
    """Append a 'Screenshots' section of `images` (name, url) to `existing_text`, using raw HTML
    `<img>` tags rather than Markdown `![]()` syntax.

    Use this instead of `build_screenshots_section` wherever the target resource's Markdown-vs-HTML
    rendering isn't something the caller controls or can even see via the REST API -- Azure DevOps
    work item Comments are the case that motivated this: the "Add Comment" REST API request body
    has no `format`/Markdown field at all (it's just `{"text": ...}`), and whether an org's
    comments render Markdown is gated by an org/tenant-level rollout the API doesn't expose, so a
    `![]()` embed can silently show as literal unrendered text instead of a picture. A raw `<img>`
    tag renders either way: a plain-HTML comment/field renders it natively, and a Markdown one
    still renders it since Markdown renderers pass inline HTML through untouched.
    """
    section = "\n".join(f'<img src="{escape(url)}" alt="{escape(name)}" style="max-width:100%;">' for name, url in images)
    return f"{existing_text or ''}\n\n<h2>Screenshots</h2>\n\n{section}\n"
