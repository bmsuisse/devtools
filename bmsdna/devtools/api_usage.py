"""The `routes` check of `bdt dead-code`: backend (FastAPI) operations that no non-generated frontend code calls
and no backend code references by name (`url_for`).

A route nobody calls is dead code that still has to be maintained, secured and tested. The check
needs two inventories:

- **the backend's operations** -- from the live app (`app.openapi()`, recursing into mounted
  sub-apps, run in a subprocess so two backends with a same-named package can't collide) or from a
  committed `openapi.json`;
- **the frontend's call sites** -- found in *non-generated* TypeScript/JavaScript/Vue sources only.
  Generated code (`*.gen.*`, `*.generated.*`, `generated/`, `*.d.ts`, ...), tests and e2e specs are
  never counted: a generated client lists *every* route, so counting it would make everything look called.
  Comments are blanked before scanning.

An operation counts as called when, in non-generated code of one of its app's frontends, there is
(strongest first):

- **sdk** -- a reference to a hey-api SDK function (read from that frontend's own `sdk.gen.ts`, matched on
  method *and* url) or one of its react-query helpers (`fooOptions`, `fooMutation`, `fooQueryKey`, ...);
- **fetch** -- an openapi-fetch style `.GET("/path"` literal naming this method and path;
- **url** -- a string/template literal equal to the path template (any method), e.g. a hand-written
  `fetch(`/api/x/${id}`)`, `"/api/x/" + id` or an `<a href>`;
- **url-sfx** -- a literal that is a suffix of the route (client with a base URL the scan can't see).

An operation also counts as used when backend code (Python or Jinja-style templates under the app's `app_dir`, tests
excluded) names its route -- `request.url_for("auth_callback")`, `app.url_path_for("login")`, `{{ url_for('login') }}`.
That is how auth redirects and OAuth callbacks are wired, and they have no frontend caller by nature.

Deliberately heuristic (no type information; a URL literal matches every method of its path, and so does an SPA
`<Link to="/users/${id}">`). Routes that are legitimately not called by the frontend -- external APIs, auth
redirects, health checks, service-to-service calls, LLM/MCP tools -- are excluded by prefix/tag/glob, or ratcheted
through a baseline file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from . import _api_dump
from .lint import _DEFAULT_EXCLUDE_DIR_NAMES, _iter_files
from .lint_findings import Finding
from .lint_typescript import _mask, is_excluded_ts_file

RULE = "api-route-uncalled"
RULE_STALE_BASELINE = "api-route-baseline-stale"

SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".js", ".jsx", ".vue")
_EXCLUDE_DIR_NAMES = _DEFAULT_EXCLUDE_DIR_NAMES | {".output", ".nuxt", ".next", ".turbo", "storybook-static"}
# `is_excluded_ts_file` only knows .ts/.tsx/.mts names; the same generated/test conventions apply to JS and Vue files.
_EXCLUDE_FILE_RE = re.compile(r"\.(?:gen|generated|test|spec)\.(?:[cm]?[jt]sx?|vue)$|\.d\.[cm]?ts$")
_EXCLUDE_FILE_PREFIXES = ("api-types", "openapi_schema")
_REACT_QUERY_SUFFIXES = ("Options", "Mutation", "InfiniteOptions", "QueryKey", "InfiniteQueryKey", "Query")
_NAME_SOURCE_SUFFIXES = (".py", ".html", ".htm", ".jinja", ".jinja2", ".j2")
_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__"})
_TEST_FILE_RE = re.compile(r"^(?:test_.*|.*_test|conftest)\.py$")
DEFAULT_URL_FOR_FUNCTIONS = ("url_for", "url_path_for")
_APP_IMPORT_TIMEOUT_SECONDS = 300
_MAX_TEMPLATE_CHARS = 4000  # a URL template longer than this is not a URL; also bounds the rescan of an unclosed backtick
_MAX_TEMPLATE_DEPTH = 20

# Bounded character classes keep these linear on garbage input (a lone `'${` must not scan to end of file).
_QUOTED_PATH_RE = re.compile(r"""(?P<q>['"])(?P<body>(?:\$\{[^}\n'"]*\})*/[^'"\n]*)(?P=q)""")
_TEMPLATE_START_RE = re.compile(r"`(?=(?:\$\{[^}`\n]*\})*/)")
_FETCH_METHOD_RE = re.compile(r"\.(GET|POST|PUT|PATCH|DELETE)\s*(?:<[^()]*?>)?\(\s*$")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_SDK_FN_RE = re.compile(r"export const (\w+) = ")
_SDK_URL_RE = re.compile(r"""\.(get|post|put|patch|delete|head|options)\b[^;]{0,4000}?url:\s*["']([^"']+)["']""", re.S)


class ApiUsageError(Exception):
    """A configuration or environment problem (not a lint finding): the app wouldn't import, a path is missing, ..."""


@dataclass(frozen=True, slots=True)
class Operation:
    method: str  # upper-case
    path: str  # full path including any mount prefix, FastAPI `{param}` style
    tags: tuple[str, ...] = ()
    mount: str = ""  # the sub-app mount prefix part of `path` ("" for the root app)
    operation_id: str = ""  # OpenAPI operationId; carries the route name FastAPI derives it from (see `is_named_by`)

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    @classmethod
    def from_dict(cls, d: dict) -> Operation:
        return cls(d["method"], d["path"], tuple(d["tags"]), d["mount"], d.get("operation_id", ""))


# --------------------------------------------------------------------------- backend inventory


def load_app_operations(app_spec: str, *, cwd: Path, env: dict[str, str] | None = None) -> list[Operation]:
    """Import `module:attr` in a fresh interpreter (same venv as bdt) with `cwd` as the working/import
    directory, and return its operations. The child runs `_api_dump`'s source via `-c` instead of importing
    anything from this package, so a repo-local `bmsdna` package can't shadow `bmsdna.devtools`."""
    if ":" not in app_spec:
        raise ApiUsageError(f"app '{app_spec}' must look like 'module.path:attribute'")
    if not cwd.is_dir():
        raise ApiUsageError(f"app_dir '{cwd}' is not a directory")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ops.json"
        try:
            proc = subprocess.run(
                [sys.executable, "-c", Path(_api_dump.__file__).read_text(encoding="utf-8"), app_spec, str(out)],
                cwd=cwd,
                env={**os.environ, **(env or {})},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=_APP_IMPORT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ApiUsageError(f"importing '{app_spec}' took longer than {_APP_IMPORT_TIMEOUT_SECONDS}s (cwd {cwd})") from exc
        if proc.returncode != 0 or not out.is_file():
            tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-15:])
            raise ApiUsageError(f"could not load operations from '{app_spec}' (cwd {cwd}):\n{tail}")
        return [Operation.from_dict(d) for d in json.loads(out.read_text(encoding="utf-8"))]


def load_openapi_file(path: Path) -> list[Operation]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApiUsageError(f"could not read OpenAPI file {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ApiUsageError(f"OpenAPI file {path} is not a JSON object")
    return [Operation.from_dict(d) for d in _api_dump.ops_from_doc(doc)]


# --------------------------------------------------------------------------- frontend scan


def normalize_path(text: str) -> str:
    """Comparable form of a path template or URL literal: `{x}`/`${expr}` -> `{}`, query/fragment dropped,
    a trailing placeholder glued to the path (`/x${qs}`, a query suffix) dropped, no trailing slash."""
    text = re.sub(r"\$\{[^}]*\}", "{}", text)
    text = re.sub(r"\{[^}/]*\}", "{}", text)
    text = text.split("?")[0].split("#")[0]
    text = re.sub(r"(\{\})+", "{}", text)
    text = re.sub(r"(?<=[^/{}])\{\}$", "", text)
    return text.rstrip("/") or "/"


def _read_template(text: str, i: int, end: int, depth: int = 0) -> tuple[str, int]:
    """`text[i]` is an opening backtick. Returns (template text with every `${expr}` replaced by `{}`,
    index after the closing backtick), handling templates/strings/braces nested inside `${...}`. Stops at `end`
    (and at a nesting depth that only hostile input reaches) instead of scanning on."""
    out: list[str] = []
    i += 1
    while i < end:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return "".join(out), i + 1
        elif c == "$" and text.startswith("${", i):
            level, i = 1, i + 2
            while i < end and level:
                c = text[i]
                if c == "`":
                    if depth >= _MAX_TEMPLATE_DEPTH:
                        return "".join(out), end
                    _, i = _read_template(text, i, end, depth + 1)
                    continue
                if c in "'\"":
                    j = i + 1
                    while j < end and text[j] != c and text[j] != "\n":
                        j += 2 if text[j] == "\\" else 1
                    i = j + 1
                    continue
                level += (c == "{") - (c == "}")
                i += 1
            out.append("{}")
        else:
            out.append(c)
            i += 1
    return "".join(out), end


@dataclass(slots=True)
class FrontendUsage:
    files: int = 0
    identifiers: set[str] = field(default_factory=set)
    fetch_calls: set[tuple[str, str]] = field(default_factory=set)  # (METHOD, normalized path) from `.GET("/x"`
    literals: set[str] = field(default_factory=set)  # normalized path-like string/template literals
    suffix_literals: set[str] = field(default_factory=set)  # literals usable for url-sfx matching (derived, see scan_frontend)


@dataclass(slots=True)
class Frontend:
    """One frontend source directory: what its non-generated code references, and its own generated hey-api SDK
    (function -> (METHOD, normalized url)). Kept per directory so two apps that both generate `listItems` can't be confused."""

    usage: FrontendUsage
    sdk: dict[str, tuple[str, str]] = field(default_factory=dict)


def is_generated_or_test_file(path: Path, *, repo_root: Path, exclude_globs: list[str]) -> bool:
    return (
        bool(_EXCLUDE_FILE_RE.search(path.name))
        or path.name.startswith(_EXCLUDE_FILE_PREFIXES)
        or is_excluded_ts_file(path, repo_root=repo_root, exclude_globs=exclude_globs)
    )


def iter_frontend_files(directory: Path, *, repo_root: Path, exclude_globs: list[str]) -> list[Path]:
    files, _ = _iter_files([directory], _EXCLUDE_DIR_NAMES, SOURCE_SUFFIXES)
    return [f for f in files if not is_generated_or_test_file(f, repo_root=repo_root, exclude_globs=exclude_globs)]


def scan_frontend(files: list[Path]) -> FrontendUsage:
    usage = FrontendUsage()
    for path in files:
        text = _mask(path.read_text(encoding="utf-8", errors="ignore")).code  # comments blanked, strings kept
        usage.files += 1
        usage.identifiers.update(_IDENT_RE.findall(text))
        found: list[tuple[int, str]] = [(m.start(), m.group("body")) for m in _QUOTED_PATH_RE.finditer(text)]
        for m in _TEMPLATE_START_RE.finditer(text):
            found.append((m.start(), _read_template(text, m.start(), min(len(text), m.start() + _MAX_TEMPLATE_CHARS))[0]))
        for pos, body in found:
            normalized = normalize_path(body)
            call = _FETCH_METHOD_RE.search(text[max(0, pos - 40) : pos])
            if call:  # method-exact evidence only; it must not also count as a method-agnostic URL literal
                usage.fetch_calls.add((call.group(1), normalized))
                continue
            if normalized != "/":
                usage.literals.add(normalized)
            if body.endswith("/") and body != "/":  # `"/api/items/" + id`
                usage.literals.add(normalize_path(body + "{}"))
    for literal in usage.literals:
        bare = re.sub(r"^(\{\})+", "", literal)
        if bare.count("/") >= 2 and "{}" not in bare.split("/")[1:2]:
            usage.suffix_literals.add(bare)
    return usage


def read_sdk_functions(directory: Path) -> dict[str, tuple[str, str]]:
    """hey-api `sdk.gen.ts` files under `directory`: exported function name -> (METHOD, normalized url as generated)."""
    functions: dict[str, tuple[str, str]] = {}
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIR_NAMES]
        if "sdk.gen.ts" not in filenames:
            continue
        text = Path(dirpath, "sdk.gen.ts").read_text(encoding="utf-8", errors="ignore")
        for chunk in re.split(r"(?=^export const \w+ = )", text, flags=re.M):
            name, url = _SDK_FN_RE.match(chunk), _SDK_URL_RE.search(chunk)
            if name and url:
                functions[name.group(1)] = (url.group(1).upper(), normalize_path(url.group(2)))
    return functions


def build_frontend(directory: Path, *, repo_root: Path, exclude_globs: list[str]) -> Frontend:
    files = iter_frontend_files(directory, repo_root=repo_root, exclude_globs=exclude_globs)
    return Frontend(scan_frontend(files), read_sdk_functions(directory))


# --------------------------------------------------------------------------- backend references by route name


def read_referenced_route_names(directory: Path, functions: tuple[str, ...] = DEFAULT_URL_FOR_FUNCTIONS) -> set[str]:
    """Route names passed as a string literal to one of `functions` (`request.url_for("x")`, `app.url_path_for(name="x")`,
    `{{ url_for('x', id=1) }}`) in the non-test Python files and templates under `directory`. A mounted sub-app's
    `"mount:x"` yields `x`. Regex based, so a call in a comment counts too (that only ever hides a dead route, never invents one)."""
    pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, functions)) + r")\(\s*(?:\w+\s*=\s*)?(['\"])(?P<name>[^'\"\n]+)\1")
    files, _ = _iter_files([directory], _EXCLUDE_DIR_NAMES | _TEST_DIR_NAMES, _NAME_SOURCE_SUFFIXES)
    names: set[str] = set()
    for path in files:
        if _TEST_FILE_RE.match(path.name):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        names.update(m.group("name").rsplit(":", 1)[-1] for m in pattern.finditer(text))
    return names


def is_named_by(op: Operation, names: set[str]) -> bool:
    """Whether `op`'s route is one of `names`. The OpenAPI document has no route name, but the operationId is derived
    from it: `{name}{path}_{method}` with non-word characters as `_` by default (rebuilt here from the mount-relative
    path), the bare name or `<prefix>-<name>` with a custom `generate_unique_id`."""
    if not op.operation_id:
        return False
    path = op.path[len(op.mount) :] if op.mount and op.path.startswith(op.mount) else op.path
    return any(
        op.operation_id in (n, re.sub(r"\W", "_", n + path) + "_" + op.method.lower()) or op.operation_id.endswith("-" + n)
        for n in names
    )


# --------------------------------------------------------------------------- matching

_EVIDENCE_RANK = {"sdk": 0, "fetch": 1, "url": 2, "url-sfx": 3}


def _evidence_in(op: Operation, frontend: Frontend, candidates: set[str]) -> str | None:
    usage, sdk = frontend.usage, frontend.sdk
    for fn, (method, url) in sdk.items():
        if method != op.method or url not in candidates:
            continue
        if fn in usage.identifiers:
            return "sdk"
        # `fooOptions`/`fooMutation`/... -- unless that name is itself another SDK function (`getUser` vs `getUserQuery`)
        if any(fn + s in usage.identifiers and fn + s not in sdk for s in _REACT_QUERY_SUFFIXES):
            return "sdk"
    if any((op.method, c) in usage.fetch_calls for c in candidates):
        return "fetch"
    if candidates & usage.literals:
        return "url"
    if any(c.endswith(bare) for bare in usage.suffix_literals for c in candidates):
        return "url-sfx"
    return None


def call_evidence(op: Operation, frontends: list[Frontend]) -> str | None:
    """The strongest kind of evidence (`sdk`/`fetch`/`url`/`url-sfx`) that `op` is called from any of `frontends`, or None."""
    full = normalize_path(op.path)
    relative = (full[len(op.mount) :] if op.mount and full.startswith(op.mount) else full) or "/"
    candidates = {full, relative}
    found = [e for fe in frontends if (e := _evidence_in(op, fe, candidates))]
    return min(found, key=_EVIDENCE_RANK.__getitem__) if found else None


def is_excluded(op: Operation, *, prefixes: list[str], tags: list[str], path_globs: list[str]) -> bool:
    return (
        any(op.path.startswith(p) for p in prefixes)
        or any(t in tags for t in op.tags)
        or any(fnmatch(op.path, g) or fnmatch(op.key, g) for g in path_globs)
    )


# --------------------------------------------------------------------------- configuration


@dataclass(frozen=True, slots=True)
class AppConfig:
    name: str
    frontends: list[str]
    app: str | None = None
    openapi: str | None = None
    app_dir: str = "."
    env: dict[str, str] = field(default_factory=dict)
    exclude_prefixes: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    exclude_frontend_globs: list[str] = field(default_factory=list)
    url_for_functions: list[str] = field(default_factory=lambda: list(DEFAULT_URL_FOR_FUNCTIONS))
    baseline: str | None = None


def _str_list(raw: dict, key: str, app: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ApiUsageError(f"dead_code app '{app}': `{key}` must be a list of strings, got {value!r}")
    return value


def parse_config(table: dict) -> list[AppConfig]:
    apps = table.get("apps") or []
    if not apps:
        raise ApiUsageError("no [[tool.bdt.dead_code.apps]] configured in pyproject.toml")
    configs: list[AppConfig] = []
    for i, raw in enumerate(apps):
        name = str(raw.get("name") or raw.get("app") or raw.get("openapi") or f"app{i}")
        if bool(raw.get("app")) == bool(raw.get("openapi")):
            raise ApiUsageError(f"dead_code app '{name}': set exactly one of `app` (module:attr) and `openapi` (path to openapi.json)")
        frontends = _str_list(raw, "frontends", name)
        if not frontends:
            raise ApiUsageError(f"dead_code app '{name}': `frontends` (list of frontend source dirs/globs) is required")
        env = raw.get("env") or {}
        if not isinstance(env, dict):
            raise ApiUsageError(f"dead_code app '{name}': `env` must be a table")
        configs.append(
            AppConfig(
                name=name,
                frontends=frontends,
                app=raw.get("app"),
                openapi=raw.get("openapi"),
                app_dir=str(raw.get("app_dir", ".")),
                env={str(k): str(v) for k, v in env.items()},
                exclude_prefixes=_str_list(raw, "exclude_prefixes", name),
                exclude_tags=_str_list(raw, "exclude_tags", name),
                exclude_paths=_str_list(raw, "exclude_paths", name),
                exclude_frontend_globs=_str_list(raw, "exclude_frontend_globs", name),
                url_for_functions=_str_list(raw, "url_for_functions", name) or list(DEFAULT_URL_FOR_FUNCTIONS),
                baseline=raw.get("baseline"),
            )
        )
    return configs


def _resolve_frontend_dirs(repo_root: Path, patterns: list[str]) -> list[Path]:
    dirs: list[Path] = []
    for pattern in patterns:
        if not pattern or Path(pattern).is_absolute():
            raise ApiUsageError(f"frontends entry '{pattern}' must be a non-empty repo-relative path or glob")
        matches = sorted(p for p in repo_root.glob(pattern) if p.is_dir())
        if not matches:
            raise ApiUsageError(f"frontends entry '{pattern}' matches no directory under {repo_root}")
        dirs.extend(matches)
    return dirs


# --------------------------------------------------------------------------- baseline


def read_baseline(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")}


def write_baseline(path: Path, keys: list[str]) -> None:
    header = "# Backend operations known to be unused (bdt dead-code). Remove a line once the route is deleted or called.\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "".join(f"{k}\n" for k in sorted(keys)), encoding="utf-8")


# --------------------------------------------------------------------------- orchestration


def check_app(config: AppConfig, *, repo_root: Path, update_baseline: bool = False) -> list[Finding]:
    if config.openapi:
        operations = load_openapi_file(repo_root / config.openapi)
    else:
        assert config.app is not None
        operations = load_app_operations(config.app, cwd=repo_root / config.app_dir, env=config.env)
    if not operations:
        raise ApiUsageError(
            f"dead_code app '{config.name}': no operations found in {config.openapi or config.app} "
            "(is it a FastAPI app with documented routes, not a wrapped/middleware ASGI app?)"
        )

    frontends = [
        build_frontend(d, repo_root=repo_root, exclude_globs=config.exclude_frontend_globs)
        for d in _resolve_frontend_dirs(repo_root, config.frontends)
    ]

    def excluded(op: Operation) -> bool:
        return is_excluded(op, prefixes=config.exclude_prefixes, tags=config.exclude_tags, path_globs=config.exclude_paths)

    names = read_referenced_route_names(repo_root / config.app_dir, tuple(config.url_for_functions))
    by_key = {op.key: op for op in operations}
    uncalled = [op for op in operations if not excluded(op) and call_evidence(op, frontends) is None and not is_named_by(op, names)]
    where = Path(config.name)

    if not config.baseline:
        return [_finding(where, op) for op in uncalled]
    baseline_path = repo_root / config.baseline
    if update_baseline:
        write_baseline(baseline_path, [op.key for op in uncalled])
        return []
    baseline = read_baseline(baseline_path)
    uncalled_keys = {op.key for op in uncalled}
    findings = [_finding(where, op) for op in uncalled if op.key not in baseline]
    for key in sorted(baseline - uncalled_keys):
        op = by_key.get(key)
        reason = "no longer a backend route" if op is None else "now excluded" if excluded(op) else "now used"
        findings.append(
            Finding(
                where,
                0,
                RULE_STALE_BASELINE,
                f"'{key}' is listed in {config.baseline} but is {reason} -- remove it (or run `bdt dead-code --update-baseline`).",
            )
        )
    return findings


def _finding(where: Path, op: Operation) -> Finding:
    tags = f" [{', '.join(op.tags)}]" if op.tags else ""
    return Finding(
        where,
        0,
        RULE,
        f"{op.key}{tags} is never called from non-generated frontend code nor referenced by url_for -- delete it, or "
        "exclude it (exclude_prefixes/exclude_tags/exclude_paths) if something other than the frontend calls it.",
    )


def run(table: dict, *, repo_root: Path, update_baseline: bool = False) -> list[Finding]:
    configs = parse_config(table)
    if update_baseline:  # validate everything first so a late failure can't leave some baselines rewritten and others not
        paths = [c.baseline for c in configs]
        if not all(paths):
            missing = ", ".join(c.name for c in configs if not c.baseline)
            raise ApiUsageError(f"--update-baseline needs a `baseline` for every app; missing for: {missing}")
        if len(set(paths)) != len(paths):
            raise ApiUsageError("--update-baseline: two apps share the same `baseline` file, each would overwrite the other")
    findings: list[Finding] = []
    for config in configs:
        findings.extend(check_app(config, repo_root=repo_root, update_baseline=update_baseline))
    return findings
