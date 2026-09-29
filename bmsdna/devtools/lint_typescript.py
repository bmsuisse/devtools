"""`bdt lint`'s TypeScript rules (bmsuisse/devtools#52): a frontend package that already
generates an API client from the backend's OpenAPI schema (openapi-typescript/openapi-fetch,
@hey-api/openapi-ts, orval, ...) should use it -- not hand-wire `fetch`/`axios` calls and
hand-write the response types. Hand-wired access stays fine for files and other non-JSON
traffic (FormData uploads, blob downloads, SSE/streams), since generators handle those badly.

There's no TypeScript parser in the dependency set, so this is a small tokenizer-level scan:
comments and string contents are blanked out (so `fetch(` inside a comment/string never
matches), then call sites are found by regex and the matching parens/enclosing block are
inspected for the non-JSON markers. Deliberately heuristic; a false positive is silenced with
a `// bdt-lint: ignore ts-handwired-http -- <reason>` comment on (or right above) the line.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .lint_findings import Finding

RULE_HTTP = "ts-handwired-http"
RULE_MODEL = "ts-handwired-model"

TS_SUFFIXES = (".ts", ".tsx", ".mts")

DEFAULT_TS_EXCLUDE_DIR_NAMES = frozenset(
    {"generated", "__generated__", "__tests__", "__mocks__", "tests", "test", "e2e", "coverage", ".next", "storybook-static"}
)
_EXCLUDE_FILE_SUFFIXES = (".gen.ts", ".generated.ts", ".d.ts", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
_EXCLUDE_FILE_NAMES = frozenset({"api-types.ts", "openapi-ts.config.ts"})

_GENERATOR_PACKAGES = frozenset(
    {
        "openapi-typescript",
        "openapi-fetch",
        "openapi-react-query",
        "@hey-api/openapi-ts",
        "orval",
        "openapi-typescript-codegen",
        "swagger-typescript-api",
        "@openapitools/openapi-generator-cli",
        "@rtk-query/codegen-openapi",
        "@kubb/core",
        "@kubb/cli",
    }
)

# Response/request shapes that generators handle badly -- a call whose enclosing block mentions
# one of these is considered a legitimate hand-wired use.
DEFAULT_NON_JSON_MARKERS = (
    "FormData",
    "Blob",
    ".blob(",
    ".arrayBuffer(",
    ".formData(",
    "getReader(",
    "ReadableStream",
    "TextDecoder",
    "EventSource",
    "SSE",
    "response.body",
    "res.body",
    "resp.body",
    "text/event-stream",
    "multipart/",
    "application/octet-stream",
    "createObjectURL",
    "new File(",
)

_FETCH_RE = re.compile(r"(?<![\w$.])(?:(?:window|globalThis|self)\s*\.\s*)?fetch\s*\(")
_AXIOS_RE = re.compile(
    r"(?<![\w$.])axios\s*(?:\.\s*(?:get|post|put|patch|delete|head|options|request|create)\s*)?(?:<[^<>()]*>\s*)?\("
)
_XHR_RE = re.compile(r"(?<![\w$.])new\s+XMLHttpRequest\b")
_JSON_CAST_RE = re.compile(r"\.json\s*\(\s*\)\s*\)?\s*as\s+(?!unknown\b|const\b)")
_JSON_ANNOTATED_RE = re.compile(r":\s*[\w$.<>\[\]| ]+?\s*=\s*await\s+[\w$.]+\.json\s*\(\s*\)")
_IGNORE_RE = re.compile(r"bdt-lint:\s*ignore\s+([\w-]+(?:\s*,\s*[\w-]+)*)")
_EXTERNAL_URL_RE = re.compile(r"""^\s*[`'"](?:https?:)?//""")
_RETURNS_TEXT_RE = re.compile(r"(?:\breturn\s+(?:await\s+)?\(?|=>\s*)[\w$.]+\.text\s*\(\)")
_BARE_IDENT_RE = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*(?:,|$)")

_MAX_WINDOW_LINES = 40


@dataclass(frozen=True, slots=True)
class _Masked:
    """`skeleton`: comments and string/template contents blanked. `code`: only comments blanked
    (strings intact, for URL/marker inspection). Both are the same length as the source, newlines kept."""

    skeleton: str
    code: str


def _mask(text: str) -> _Masked:
    n = len(text)
    skeleton = list(text)
    code = list(text)

    def blank(chars: list[str], start: int, end: int) -> None:
        for k in range(start, end):
            if chars[k] != "\n":
                chars[k] = " "

    # Stack entries: "tpl" (inside a template literal's text) or an int (brace depth inside a `${ }`).
    stack: list[str | int] = []
    i = 0
    while i < n:
        top = stack[-1] if stack else None
        ch = text[i]

        if top == "tpl":
            if ch == "\\":
                blank(skeleton, i, min(i + 2, n))
                i += 2
            elif ch == "`":
                stack.pop()
                i += 1
            elif ch == "$" and text.startswith("${", i):
                stack.append(0)
                i += 2
            else:
                blank(skeleton, i, i + 1)
                i += 1
            continue

        if ch == "/" and text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            blank(skeleton, i, end)
            blank(code, i, end)
            i = end
        elif ch == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            blank(skeleton, i, end)
            blank(code, i, end)
            i = end
        elif ch in "'\"":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j, n)
            # Only the contents are blanked in the skeleton; the quotes stay so the shape is visible.
            blank(skeleton, i + 1, j)
            i = j + 1 if j < n and text[j] == ch else j
        elif ch == "`":
            stack.append("tpl")
            i += 1
        elif isinstance(top, int):
            if ch == "{":
                stack[-1] = top + 1
            elif ch == "}":
                if top == 0:
                    stack.pop()
                else:
                    stack[-1] = top - 1
            i += 1
        else:
            i += 1

    return _Masked("".join(skeleton), "".join(code))


def _matching_paren(skeleton: str, open_idx: int) -> int:
    depth = 0
    for k in range(open_idx, len(skeleton)):
        c = skeleton[k]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return k
    return len(skeleton)


def _has_marker(window: str, markers: tuple[str, ...]) -> bool:
    """Alphanumeric markers match whole words only (so `SSE` doesn't fire on `ASSET`)."""
    for marker in markers:
        pattern = rf"\b{re.escape(marker)}\b" if marker.isalnum() else re.escape(marker)
        if re.search(pattern, window):
            return True
    return False


def _is_external(args: str, window: str) -> bool:
    """The first argument is (or is a variable initialised to, within `window`) an absolute/protocol-relative URL."""
    if _EXTERNAL_URL_RE.match(args):
        return True
    ident = _BARE_IDENT_RE.match(args)
    if ident is None:
        return False
    return re.search(rf"\b{re.escape(ident.group(1))}\b\s*(?::[^=;]+)?=\s*[`'\"](?:https?:)?//", window) is not None


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _block_window(lines: list[str], call_line: int) -> tuple[int, int]:
    """1-based (first, last) line of the block enclosing `call_line` (each direction capped): the
    contiguous lines around it that are indented at least as deep as the call's own line, plus the
    header line just above (so a marker in the function signature, e.g. `file: File`, counts).
    A call at column 0 gets just its own line -- there's no enclosing block to speak of."""
    base = _indent(lines[call_line - 1])
    if base == 0:
        return call_line, call_line
    first = call_line
    for k in range(call_line - 2, max(call_line - 2 - _MAX_WINDOW_LINES, -1), -1):
        if lines[k].strip() and _indent(lines[k]) < base:
            first = k + 1  # the header line
            break
        first = k + 1
    last = call_line
    for k in range(call_line, min(len(lines), call_line - 1 + _MAX_WINDOW_LINES)):
        if lines[k].strip() and _indent(lines[k]) < base:
            break
        last = k + 1
    return first, last


def _ignored(raw_lines: list[str], line: int, rule: str) -> bool:
    for candidate in (line, line - 1):
        if 1 <= candidate <= len(raw_lines):
            match = _IGNORE_RE.search(raw_lines[candidate - 1])
            if match and rule in {r.strip() for r in match.group(1).split(",")}:
                return True
    return False


def is_excluded_ts_file(path: Path, *, repo_root: Path, exclude_globs: list[str] | None = None) -> bool:
    """Generated code, tests, declaration files and user-configured globs are never scanned --
    this also applies to files passed explicitly (e.g. by a pre-commit hook's staged-file list)."""
    name = path.name
    if name in _EXCLUDE_FILE_NAMES or name.endswith(_EXCLUDE_FILE_SUFFIXES):
        return True
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        rel = path
    if any(part in DEFAULT_TS_EXCLUDE_DIR_NAMES for part in rel.parts[:-1]):
        return True
    rel_posix = rel.as_posix()
    return any(fnmatch(rel_posix, pattern) for pattern in exclude_globs or [])


def find_generator(path: Path, *, repo_root: Path, cache: dict[Path, str | None]) -> str | None:
    """The API-client generator the nearest `package.json` above `path` declares (its dependencies
    or an `openapi` script), or None -- meaning there's no generated client to use instead."""
    try:
        root = repo_root.resolve()
    except OSError:
        root = repo_root
    directory = path.resolve().parent
    while True:
        package_json = directory / "package.json"
        if package_json.is_file():
            if package_json not in cache:
                cache[package_json] = _read_generator(package_json)
            return cache[package_json]
        if directory == root or directory.parent == directory:
            return None
        directory = directory.parent


def _read_generator(package_json: Path) -> str | None:
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    names = set()
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            names.update(section)
    found = sorted(names & _GENERATOR_PACKAGES)
    if found:
        return found[0]
    scripts = data.get("scripts")
    if isinstance(scripts, dict) and any("openapi" in str(v) for v in scripts.values()):
        return "an openapi script"
    return None


def check_typescript_file(
    path: Path,
    *,
    generator: str,
    source: str | None = None,
    non_json_markers: tuple[str, ...] = DEFAULT_NON_JSON_MARKERS,
) -> list[Finding]:
    if source is not None:
        text = source
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []

    masked = _mask(text)
    raw_lines = text.split("\n")
    code_lines = masked.code.split("\n")
    findings: list[Finding] = []
    covered: list[tuple[int, int]] = []  # line ranges of http call windows (model rule skips these)

    def consider(kind: str, call_start: int, args_start: int | None) -> None:
        line = _line_of(text, call_start)
        first_line, end_line = _block_window(raw_lines, line)
        covered.append((first_line, end_line))
        if args_start is not None:
            close = _matching_paren(masked.skeleton, args_start)
            args = masked.code[args_start + 1 : close]
            end_line = max(end_line, _line_of(text, close))
        window = "\n".join(code_lines[first_line - 1 : end_line])
        if args_start is not None and _is_external(args, window):
            return  # a third-party URL -- there's no generated client for it
        if _has_marker(window, non_json_markers):
            return
        if _RETURNS_TEXT_RE.search(window) and ".json(" not in window:
            return  # plain-text/markdown response, not JSON
        if _ignored(raw_lines, line, RULE_HTTP):
            return
        findings.append(
            Finding(
                path,
                line,
                RULE_HTTP,
                f"hand-wired {kind} instead of the generated API client ({generator}) -- call the generated client so "
                "paths and response types stay in sync with the backend's OpenAPI schema. Hand-wired access is only OK "
                "for files/FormData/blobs/streams; otherwise silence with "
                f"`// bdt-lint: ignore {RULE_HTTP} -- <reason>`.",
            )
        )

    for match in _FETCH_RE.finditer(masked.skeleton):
        consider("fetch()", match.start(), match.end() - 1)
    for match in _AXIOS_RE.finditer(masked.skeleton):
        consider("axios call", match.start(), match.end() - 1)
    for match in _XHR_RE.finditer(masked.skeleton):
        consider("XMLHttpRequest", match.start(), None)

    for regex in (_JSON_CAST_RE, _JSON_ANNOTATED_RE):
        for match in regex.finditer(masked.skeleton):
            line = _line_of(text, match.start())
            if any(lo <= line <= hi for lo, hi in covered) or _ignored(raw_lines, line, RULE_MODEL):
                continue
            findings.append(
                Finding(
                    path,
                    line,
                    RULE_MODEL,
                    f"response `.json()` is typed by hand-writing/casting a model -- use the type generated by {generator} "
                    f"instead (or `// bdt-lint: ignore {RULE_MODEL} -- <reason>`).",
                )
            )

    return findings
