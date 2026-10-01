"""Shared `Finding` type + rendering for every `bdt lint`/`bdt lint-api-usage` rule module (lint_sql,
lint_models, lint_typescript, lint_sql_files, lint_tooling, api_usage) -- kept separate from lint.py so each rule module
only depends on this, not on the orchestrator (which depends on all of them).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Finding:
    """One lint violation. `line` is 1-based; 0 means "applies to the whole
    file/repo, not a specific line" (e.g. a missing-tooling-config finding).
    """

    path: Path
    line: int
    rule: str
    message: str
    severity: str = "error"  # "error" | "review" (needs a human/AI to verify; `bdt find-injection` only)

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else str(self.path)
        return f"{location}: [{self.rule}] {self.message}"


def render_findings(findings: list[Finding]) -> str:
    return "\n".join(f.render() for f in sorted(findings, key=lambda f: (f.path, f.line, f.rule)))
