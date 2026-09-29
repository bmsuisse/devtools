"""`bdt find-injection`'s frontend rules (bmsuisse/devtools#54): DOM-XSS sinks in TS/JS/JSX/Vue
(comments and string contents are masked with lint_typescript's tokenizer so a mention inside
a comment or string never matches), un-sandboxed iframes, and Content-Security-Policy checks.

Deliberately heuristic, like lint_typescript: no JS parser. Findings that depend on where a
value comes from are "review" items.
"""

from __future__ import annotations

import re
from pathlib import Path

from .lint_findings import Finding
from .lint_typescript import _line_of, _mask

FRONTEND_SUFFIXES = (
    ".ts",
    ".tsx",
    ".mts",
    ".js",
    ".jsx",
    ".mjs",
    ".vue",
    ".svelte",
    ".html",
    ".htm",
)
_HTML_SUFFIXES = (".html", ".htm")
_MASKABLE_SUFFIXES = (".ts", ".tsx", ".mts", ".js", ".jsx", ".mjs")

_CSP_RE = re.compile(r"content-security-policy", re.IGNORECASE)
_CSP_WEAK_RE = re.compile(r"'unsafe-(?:inline|eval)'|(?:script|default)-src[^;\n]*\s\*(?=[\s;\"'`]|$)", re.IGNORECASE)

_INNER_HTML_ASSIGN_RE = re.compile(r"\.\s*(?:inner|outer)HTML\s*\+?=(?!=)")
_INSERT_HTML_RE = re.compile(r"\.\s*insertAdjacentHTML\s*\(")
_DOC_WRITE_RE = re.compile(r"(?<![\w$])document\s*\.\s*write(?:ln)?\s*\(")
_EVAL_RE = re.compile(r"(?<![\w$.])eval\s*\(")
_NEW_FUNCTION_RE = re.compile(r"(?<![\w$.])new\s+Function\s*\(")
_TIMER_STRING_RE = re.compile(r"(?<![\w$.])(?:setTimeout|setInterval)\s*\(\s*(?:['\"`]|[\w$.]+\s*\+)")
_DANGEROUS_HTML_RE = re.compile(r"\bdangerouslySetInnerHTML\b")
_V_HTML_RE = re.compile(r"\bv-html\s*=|\{@html\b")
_JS_URL_ATTR_RE = re.compile(r"""(?:href|src|action|formaction)\s*=\s*\{?\s*[`'"]\s*javascript\s*:""", re.IGNORECASE)
_POST_MESSAGE_STAR_RE = re.compile(r"""postMessage\s*\([^;]*?,\s*['"]\*['"]\s*[,)]""")
_IFRAME_RE = re.compile(r"<iframe\b", re.IGNORECASE)
_SRCDOC_RE = re.compile(r"\bsrc[dD]oc\b\s*=")


def _sink(path: Path, text: str, pos: int, rule: str, message: str, severity: str) -> Finding:
    return Finding(path, _line_of(text, pos), rule, message, severity=severity)


def _iframe_tag(code: str, start: int) -> str:
    """The `<iframe ...>` opening tag (attribute values may contain `>` inside `{...}` or quotes)."""
    depth = 0
    quote = ""
    for k in range(start, len(code)):
        c = code[k]
        if quote:
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == ">" and depth == 0:
            return code[start : k + 1]
    return code[start:]


def _check_iframes(path: Path, code: str, skeleton: str) -> list[Finding]:
    findings: list[Finding] = []
    for match in _IFRAME_RE.finditer(skeleton):
        tag = _iframe_tag(code, match.start())
        sandbox = re.search(
            r"(?<=\s)sandbox(?![\w-])(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|\{\s*[\"'`]([^\"'`]*)[\"'`]\s*\}))?",
            tag,
        )
        if sandbox is None:
            findings.append(
                _sink(
                    path,
                    code,
                    match.start(),
                    "fe-iframe-no-sandbox",
                    "`<iframe>` without a `sandbox` attribute -- embedded content runs with the page's full privileges. "
                    "Add `sandbox` (with only the allow-* flags it needs).",
                    "error",
                )
            )
            continue
        flags = next((g for g in sandbox.groups() if g), "")
        if "allow-scripts" in flags and "allow-same-origin" in flags:
            findings.append(
                _sink(
                    path,
                    code,
                    match.start(),
                    "fe-iframe-sandbox-escape",
                    "`sandbox` combines `allow-scripts` and `allow-same-origin`, which lets the framed page remove its own sandbox.",
                    "error",
                )
            )
    return findings


def check_frontend_file(path: Path, source: str | None = None) -> list[Finding]:
    if source is None:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            return []
    full_text: str = source
    suffix = path.suffix.lower()
    if suffix in _MASKABLE_SUFFIXES:
        masked = _mask(source)
        skeleton, code = masked.skeleton, masked.code
    else:  # .vue/.svelte/.html: `//` appears in URLs and `'` in prose, so the JS tokenizer would mangle them
        skeleton = code = source

    findings: list[Finding] = []

    def scan(
        regex: re.Pattern[str],
        rule: str,
        message: str,
        severity: str,
        text: str | None = None,
    ) -> None:
        haystack = skeleton if text is None else text
        findings.extend(_sink(path, full_text, m.start(), rule, message, severity) for m in regex.finditer(haystack))

    if suffix not in _HTML_SUFFIXES:
        scan(
            _INNER_HTML_ASSIGN_RE,
            "fe-inner-html",
            "Assigning to `innerHTML`/`outerHTML` -- XSS if the value contains user data. AI reviewer: verify it's sanitised "
            "(e.g. DOMPurify) or use `textContent`.",
            "review",
        )
        scan(
            _INSERT_HTML_RE,
            "fe-inner-html",
            "`insertAdjacentHTML` parses its argument as HTML -- XSS if it contains user data. AI reviewer: verify it's sanitised.",
            "review",
        )
        scan(
            _DOC_WRITE_RE,
            "fe-document-write",
            "`document.write()` injects raw HTML -- avoid it.",
            "error",
        )
        scan(
            _EVAL_RE,
            "fe-eval",
            "`eval()` executes arbitrary code -- remove it.",
            "error",
        )
        scan(
            _NEW_FUNCTION_RE,
            "fe-new-function",
            "`new Function()` is `eval` in disguise -- remove it.",
            "error",
        )
        scan(
            _TIMER_STRING_RE,
            "fe-timer-string",
            "`setTimeout`/`setInterval` with a string argument evaluates it as code -- pass a function.",
            "error",
        )
        scan(
            _DANGEROUS_HTML_RE,
            "fe-dangerously-set-inner-html",
            "`dangerouslySetInnerHTML` -- XSS if the HTML contains user data. AI reviewer: verify it's sanitised (DOMPurify) or static.",
            "review",
        )
        scan(
            _POST_MESSAGE_STAR_RE,
            "fe-post-message-star",
            "`postMessage(..., '*')` sends to any origin -- pass the exact target origin.",
            "review",
            code,
        )
        scan(
            _SRCDOC_RE,
            "fe-iframe-srcdoc",
            "`srcdoc` renders an HTML string inside a frame -- verify no user data reaches it.",
            "review",
        )
        findings.extend(_check_iframes(path, code, skeleton))
    else:
        findings.extend(_check_iframes(path, code, code))

    if suffix in (".vue", ".svelte"):
        scan(
            _V_HTML_RE,
            "fe-v-html",
            "`v-html`/`{@html}` renders raw HTML -- XSS if it contains user data. AI reviewer: verify it's sanitised.",
            "review",
        )
    scan(
        _JS_URL_ATTR_RE,
        "fe-javascript-url",
        "`javascript:` URL executes code on click -- use an event handler.",
        "error",
        code,
    )
    return findings


def find_csp_weakening(path: Path, source: str | None = None) -> list[Finding]:
    """`'unsafe-inline'`/`'unsafe-eval'`/wildcard script-src inside a file that defines a CSP."""
    if source is None:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            return []
    if not _CSP_RE.search(source) and "script-src" not in source.lower() and "default-src" not in source.lower():
        return []
    return [
        _sink(
            path,
            source,
            m.start(),
            "csp-weakened",
            f"CSP contains `{m.group(0).strip()}`, which weakens XSS protection. AI reviewer: verify this relaxation is necessary "
            "(prefer nonces/hashes).",
            "review",
        )
        for m in _CSP_WEAK_RE.finditer(source)
    ]


def has_csp(source: str) -> bool:
    return _CSP_RE.search(source) is not None
