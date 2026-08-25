"""Markdown building shared by the Azure DevOps and GitHub screenshot-attach paths."""

from __future__ import annotations


def build_screenshots_section(existing_text: str | None, images: list[tuple[str, str]]) -> str:
    """Append a '## Screenshots' markdown section of `images` (name, url) to `existing_text`."""
    section = "\n".join(f"![{name}]({url})" for name, url in images)
    return f"{existing_text or ''}\n\n## Screenshots\n\n{section}\n"
