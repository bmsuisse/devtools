"""`bdt find-injection` (bmsuisse/devtools#54): scan a folder, explicit files, or a git diff for
injection risks in both backend (Python) and frontend (TS/JS/HTML/Vue) code, and warn when a
web project has no Content-Security-Policy.

Findings come in two severities: "error" (a definite unsafe pattern) and "review" (depends on
where a value comes from -- a human or AI has to verify it). Exit code 1 for any error;
review items only fail the run with `strict=True`.

A finding is silenced with `# bdt-lint: ignore <rule>` (Python) or `// bdt-lint: ignore <rule>`
(TS/JS) on, or on the comment line directly above, the flagged line.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .injection_frontend import (
    FRONTEND_SUFFIXES,
    check_frontend_file,
    find_csp_weakening,
    has_csp,
)
from .injection_python import check_python_sinks
from .lint import _DEFAULT_EXCLUDE_DIR_NAMES, _iter_files
from .lint_findings import Finding, render_findings
from .lint_sql import check_sql_file, require_sqlglot
from .lint_typescript import DEFAULT_TS_EXCLUDE_DIR_NAMES, is_excluded_ts_file

SQL_INJECTION_RULES = frozenset(
    {
        "sql-fstring-injection",
        "sql-percent-format-injection",
        "sql-concat-injection",
        "sql-format-injection",
    }
)
_IGNORE_RE = re.compile(r"bdt-lint:\s*ignore\s+([\w-]+(?:\s*,\s*[\w-]+)*)")
_WEB_FRAMEWORK_RE = re.compile(
    r"^\s*(?:from|import)\s+(?:fastapi|flask|starlette|django|aiohttp|quart|sanic|litestar)\b",
    re.MULTILINE,
)
_CONFIG_SUFFIXES = (
    ".json",
    ".conf",
    ".config",
    ".yaml",
    ".yml",
    ".toml",
    ".cjs",
    ".mjs",
    ".js",
    ".ts",
    ".py",
    ".html",
    ".htm",
    ".tsx",
)
_CONFIG_NAMES = frozenset(
    {
        "web.config",
        "nginx.conf",
        "staticwebapp.config.json",
        "_headers",
        "Caddyfile",
        ".htaccess",
    }
)
_EXCLUDE_TEST_DIR_NAMES = DEFAULT_TS_EXCLUDE_DIR_NAMES - {"generated", "__generated__"}
_SKIP_NAMES = frozenset({"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock", "npm-shrinkwrap.json"})
_MAX_CSP_FILE_BYTES = 1_000_000
_TS_LIKE = (".ts", ".tsx", ".mts", ".js", ".jsx", ".mjs")

AI_INSTRUCTION = (
    "AI reviewer: for every [review] item above, open the code, trace where the value comes from, and decide whether "
    "attacker-controlled input can reach it. Fix real risks (parameterise / sanitise / sandbox); for confirmed-safe cases add "
    "`# bdt-lint: ignore <rule>` (Python) or `// bdt-lint: ignore <rule>` (TS/JS) with a one-line reason."
)


@dataclass(frozen=True, slots=True)
class InjectionResult:
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def reviews(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "review"]


def _git(args: list[str], cwd: Path) -> str:
    try:
        return subprocess.check_output(["git", *args], encoding="utf-8", cwd=cwd, stderr=subprocess.PIPE)
    except FileNotFoundError:
        sys.exit("'git' is required for --diff but wasn't found on PATH.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"git {' '.join(args)} failed: {(e.stderr or '').strip()}")


def diff_files(root: Path, base: str | None = None) -> list[Path]:
    """Files added/changed on this branch vs `base` (default: origin's default branch, else main), plus
    uncommitted and untracked ones. Deleted files are skipped."""
    repo = Path(_git(["rev-parse", "--show-toplevel"], root).strip())
    candidates = [base] if base else []
    if not base:
        head = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
        )
        candidates += [head.stdout.strip()] if head.returncode == 0 and head.stdout.strip() else []
        candidates += ["origin/main", "main", "origin/master", "master"]
    chosen = next(
        (c for c in candidates if c and subprocess.run(["git", "rev-parse", "--verify", "-q", c], cwd=root, capture_output=True).returncode == 0),
        None,
    )
    if chosen is None:
        sys.exit(f"Couldn't find a base branch to diff against (tried {', '.join(c for c in candidates if c)}); pass --base.")
    names: set[str] = set()
    names.update(_git(["diff", "--name-only", "-z", "--diff-filter=ACMR", f"{chosen}...HEAD"], root).split("\0"))
    names.update(_git(["diff", "--name-only", "-z", "--diff-filter=ACMR", "HEAD"], root).split("\0"))
    names.update(_git(["ls-files", "--others", "--exclude-standard", "--full-name", "-z"], root).split("\0"))
    return sorted(p for p in (repo / n for n in names if n) if p.is_file())


def _pragma_ignored(lines: list[str], finding: Finding) -> bool:
    if finding.line <= 0:
        return False
    for number in (finding.line, finding.line - 1):
        if not 1 <= number <= len(lines):
            continue
        text = lines[number - 1]
        if number != finding.line and not text.lstrip().startswith(("#", "//", "/*", "*", "<!--")):
            continue
        match = _IGNORE_RE.search(text)
        if match and finding.rule in {r.strip() for r in match.group(1).split(",")}:
            return True
    return False


def _filter_ignored(findings: list[Finding]) -> list[Finding]:
    cache: dict[Path, list[str]] = {}
    kept: list[Finding] = []
    for finding in findings:
        if finding.path not in cache:
            try:
                cache[finding.path] = finding.path.read_text(encoding="utf-8").splitlines()
            except OSError, UnicodeDecodeError:
                cache[finding.path] = []
        if not _pragma_ignored(cache[finding.path], finding):
            kept.append(finding)
    return kept


def _scan_python(path: Path) -> list[Finding]:
    sql = [f for f in check_sql_file(path, review=True) if f.rule in SQL_INJECTION_RULES or f.rule in ("sql-unverified-call", "sql-unverified-cast")]
    return sql + check_python_sinks(path)


def _walk_repo_text_files(root: Path, exclude_dirs: frozenset[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for name in filenames:
            if name in _SKIP_NAMES or name.endswith(".min.js") or not (name in _CONFIG_NAMES or name.endswith(_CONFIG_SUFFIXES)):
                continue
            candidate = Path(dirpath, name)
            try:
                if candidate.stat().st_size <= _MAX_CSP_FILE_BYTES:
                    yield candidate
            except OSError:
                continue


def check_csp(root: Path, scanned: list[Path], exclude_dirs: frozenset[str], only: set[Path] | None = None) -> list[Finding]:
    """Warns when the project serves a web UI/API (frontend files scanned, or a Python web framework
    imported) but no file anywhere under `root` mentions a Content-Security-Policy. Always searches the
    whole repo -- a diff that doesn't touch the header config mustn't look like "no CSP configured".
    `only` (diff mode) restricts `csp-weakened` findings to the changed files."""
    is_web = any(p.suffix.lower() in FRONTEND_SUFFIXES for p in scanned)
    if not is_web:
        for p in scanned:
            if p.suffix == ".py":
                try:
                    if _WEB_FRAMEWORK_RE.search(p.read_text(encoding="utf-8")):
                        is_web = True
                        break
                except OSError, UnicodeDecodeError:
                    continue
    if not is_web:
        return []
    weakened: list[Finding] = []
    found = False
    for candidate in _walk_repo_text_files(root, exclude_dirs):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        if has_csp(text):
            found = True
            weakened.extend(find_csp_weakening(candidate, text))
    if found:
        return [f for f in weakened if only is None or f.path.resolve() in only]
    return [
        Finding(
            root,
            0,
            "csp-missing",
            "No Content-Security-Policy configured anywhere in the repo (response header, `<meta http-equiv>`, "
            "staticwebapp.config.json, nginx/web.config, ...). A CSP is the main defence-in-depth against XSS -- add one "
            "(start with `default-src 'self'` and no 'unsafe-inline'/'unsafe-eval').",
            severity="review",
        )
    ]


def run(
    paths: list[str],
    *,
    root: Path | None = None,
    diff: bool = False,
    base: str | None = None,
) -> InjectionResult:
    root = (root or Path.cwd()).resolve()
    exclude_dirs = _DEFAULT_EXCLUDE_DIR_NAMES | {
        ".next",
        "coverage",
        "storybook-static",
    }
    findings: list[Finding] = []
    changed: list[Path] = []

    if diff:
        changed = [p for p in diff_files(root, base) if not _in_excluded_dir(p, root, exclude_dirs)]
        py_files = [p for p in changed if p.suffix == ".py"]
        fe_files = [p for p in changed if p.suffix.lower() in FRONTEND_SUFFIXES]
    else:
        targets = [Path(p).resolve() for p in paths] if paths else [root]
        py_files, missing = _iter_files(targets, exclude_dirs, (".py",))
        fe_files, _ = _iter_files(targets, exclude_dirs, FRONTEND_SUFFIXES)
        findings.extend(
            Finding(
                p,
                0,
                "path-not-found",
                f"'{p}' doesn't exist -- nothing was scanned for it.",
            )
            for p in missing
        )

    py_files = [p for p in py_files if not _is_test_file(p, root)]
    fe_files = [p for p in fe_files if not _is_test_file(p, root)]

    if py_files:
        require_sqlglot()
    for path in py_files:
        findings.extend(_scan_python(path))
    for path in fe_files:
        findings.extend(check_frontend_file(path))

    findings.extend(check_csp(root, py_files + fe_files, exclude_dirs, {p.resolve() for p in changed} if diff else None))
    return InjectionResult(findings=_filter_ignored(findings))


def _in_excluded_dir(path: Path, root: Path, exclude_dirs: frozenset[str]) -> bool:
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        parts = path.parts[:-1]
    return any(part in exclude_dirs for part in parts)


def _is_test_file(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        parts = ()
    name = path.name
    return (
        any(part in _EXCLUDE_TEST_DIR_NAMES for part in parts)
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or (path.suffix in _TS_LIKE and is_excluded_ts_file(path, repo_root=root))
    )


def print_report(result: InjectionResult, *, strict: bool = False) -> int:
    """Prints the report and returns the exit code (1 on any error, or on any review item with `strict`)."""
    if not result.findings:
        print("bdt find-injection: no injection risks found")
        return 0
    errors, reviews = result.errors, result.reviews
    if errors:
        print("Errors:")
        print(render_findings(errors))
    if reviews:
        print(("\n" if errors else "") + "Review (verify these are safe):")
        print(render_findings(reviews))
    print(f"\n{len(errors)} error(s), {len(reviews)} item(s) to review.")
    if reviews:
        print(AI_INSTRUCTION)
    return 1 if errors or (strict and reviews) else 0
