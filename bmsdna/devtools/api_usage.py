"""`bdt lint-api-usage`: backend (FastAPI) operations that no non-generated frontend code calls.

A route nobody calls is dead code that still has to be maintained, secured and tested. The check
needs two inventories:

- **the backend's operations** -- from the live app (`app.openapi()`, recursing into mounted
  sub-apps, run in a subprocess so two backends with a same-named package can't collide) or from a
  committed `openapi.json`;
- **the frontend's call sites** -- found in *non-generated* TypeScript/JavaScript/Vue sources only.
  Generated code (`*.gen.ts`, `*.generated.*`, `generated/`, `*.d.ts`, ...), tests and e2e specs are
  never counted: a generated client lists *every* route, so counting it would make everything look called.

An operation counts as called when, in non-generated code, there is (strongest first):

- **sdk** -- a reference to a hey-api SDK function (read from `sdk.gen.ts`) or one of its react-query
  helpers (`fooOptions`, `fooMutation`, `fooQueryKey`, ...) whose generated method + url match;
- **fetch** -- an openapi-fetch style `.GET("/path"` literal naming this method and path;
- **url** -- a string/template literal equal to the path template (any method), e.g. a hand-written
  `fetch(`/api/x/${id}`)` or an `<a href>`;
- **url-sfx** -- a literal that only matches as a suffix (client with a base URL the scan can't see).

Deliberately heuristic (no type information, comments count as mentions). Routes that are legitimately
not called by the frontend -- external APIs, auth redirects, health checks, service-to-service calls,
LLM/MCP tools -- are excluded by prefix/tag/glob, or ratcheted through a baseline file.
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
from .lint_findings import Finding
from .lint_typescript import is_excluded_ts_file

RULE = "api-route-uncalled"
RULE_STALE_BASELINE = "api-route-baseline-stale"

SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".js", ".jsx", ".vue")
_SKIP_DIR_NAMES = frozenset({"node_modules", "dist", ".output", ".nuxt", ".next", ".git", ".turbo"})
_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")
_REACT_QUERY_SUFFIXES = ("Options", "Mutation", "InfiniteOptions", "QueryKey", "InfiniteQueryKey", "Query")

_QUOTED_PATH_RE = re.compile(r"""(?P<q>['"])(?P<body>(?:\$\{[^}]*\})?/[^'"\n]*)(?P=q)""")
_TEMPLATE_START_RE = re.compile(r"`(?=(?:\$\{[^}`]*\})?/)")
_FETCH_METHOD_RE = re.compile(r"\.(GET|POST|PUT|PATCH|DELETE)\s*(?:<[^()]*?>)?\(\s*$")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_SDK_FN_RE = re.compile(r"export const (\w+) = ")
_SDK_URL_RE = re.compile(r"""\.(get|post|put|patch|delete)\b[^;]*?url:\s*["']([^"']+)["']""", re.S)


class ApiUsageError(Exception):
    """A configuration or environment problem (not a lint finding): the app wouldn't import, a path is missing, ..."""


@dataclass(frozen=True, slots=True)
class Operation:
    method: str  # upper-case
    path: str  # full path including any mount prefix, FastAPI `{param}` style
    tags: tuple[str, ...] = ()
    mount: str = ""  # the sub-app mount prefix part of `path` ("" for the root app)

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


# --------------------------------------------------------------------------- backend inventory


def operations_from_openapi(doc: dict, *, mount: str = "") -> list[Operation]:
    """Operations of one OpenAPI document, with `mount` prepended to every path."""
    ops: list[Operation] = []
    for path, item in (doc.get("paths") or {}).items():
        for method, op in item.items():
            if method in _HTTP_METHODS and isinstance(op, dict):
                ops.append(Operation(method.upper(), mount + path, tuple(op.get("tags") or ()), mount))
    return ops


def collect_app_operations(app: object) -> list[Operation]:
    """Documented operations of a FastAPI-like app and, recursively, of every mounted sub-app (see `_api_dump`)."""
    return [Operation(d["method"], d["path"], tuple(d["tags"]), d["mount"]) for d in _api_dump.collect(app)]


def load_app_operations(app_spec: str, *, cwd: Path, env: dict[str, str] | None = None) -> list[Operation]:
    """Import `module:attr` in a fresh interpreter (same venv as bdt) with `cwd` as the working/import
    directory, and return its operations. The child runs `_api_dump`'s source via `-c` instead of importing
    anything from this package, so a repo-local `bmsdna` package can't shadow `bmsdna.devtools`."""
    if ":" not in app_spec:
        raise ApiUsageError(f"app '{app_spec}' must look like 'module.path:attribute'")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ops.json"
        proc = subprocess.run(
            [sys.executable, "-c", Path(_api_dump.__file__).read_text(encoding="utf-8"), app_spec, str(out)],
            cwd=cwd,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode != 0 or not out.is_file():
            tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-15:])
            raise ApiUsageError(f"could not load operations from '{app_spec}' (cwd {cwd}):\n{tail}")
        return [Operation(d["method"], d["path"], tuple(d["tags"]), d["mount"]) for d in json.loads(out.read_text(encoding="utf-8"))]


def load_openapi_file(path: Path) -> list[Operation]:
    try:
        return operations_from_openapi(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ApiUsageError(f"could not read OpenAPI file {path}: {exc}") from exc


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


def _read_template(text: str, i: int) -> tuple[str, int]:
    """`text[i]` is an opening backtick. Returns (template text with every `${expr}` replaced by `{}`,
    index after the closing backtick), handling templates/strings/braces nested inside `${...}`."""
    out: list[str] = []
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return "".join(out), i + 1
        elif c == "$" and text.startswith("${", i):
            depth, i = 1, i + 2
            while i < n and depth:
                c = text[i]
                if c == "`":
                    _, i = _read_template(text, i)
                    continue
                if c in "'\"":
                    j = i + 1
                    while j < n and text[j] != c and text[j] != "\n":
                        j += 2 if text[j] == "\\" else 1
                    i = j + 1
                    continue
                depth += (c == "{") - (c == "}")
                i += 1
            out.append("{}")
        else:
            out.append(c)
            i += 1
    return "".join(out), n


@dataclass(slots=True)
class FrontendUsage:
    files: int = 0
    identifiers: set[str] = field(default_factory=set)
    fetch_calls: set[tuple[str, str]] = field(default_factory=set)  # (METHOD, normalized path) from `.GET("/x"`
    literals: set[str] = field(default_factory=set)  # normalized path-like string/template literals


def iter_frontend_files(dirs: list[Path], *, repo_root: Path, exclude_globs: list[str]) -> list[Path]:
    files: list[Path] = []
    for directory in dirs:
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
            for name in filenames:
                path = Path(dirpath, name)
                if path.suffix in SOURCE_SUFFIXES and not is_excluded_ts_file(path, repo_root=repo_root, exclude_globs=exclude_globs):
                    files.append(path)
    return sorted(set(files))


def scan_frontend(files: list[Path]) -> FrontendUsage:
    usage = FrontendUsage()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        usage.files += 1
        usage.identifiers.update(_IDENT_RE.findall(text))
        found: list[tuple[int, str]] = [(m.start(), m.group("body")) for m in _QUOTED_PATH_RE.finditer(text)]
        found.extend((m.start(), _read_template(text, m.start())[0]) for m in _TEMPLATE_START_RE.finditer(text))
        for pos, body in found:
            normalized = normalize_path(body)
            usage.literals.add(normalized)
            call = _FETCH_METHOD_RE.search(text[max(0, pos - 40) : pos])
            if call:
                usage.fetch_calls.add((call.group(1), normalized))
    return usage


def read_sdk_functions(dirs: list[Path]) -> dict[str, tuple[str, str]]:
    """hey-api `sdk.gen.ts` under `dirs`: exported function name -> (METHOD, url as generated)."""
    functions: dict[str, tuple[str, str]] = {}
    for directory in dirs:
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
            if "sdk.gen.ts" not in filenames:
                continue
            text = Path(dirpath, "sdk.gen.ts").read_text(encoding="utf-8", errors="ignore")
            for chunk in re.split(r"(?=^export const \w+ = )", text, flags=re.M):
                name, url = _SDK_FN_RE.match(chunk), _SDK_URL_RE.search(chunk)
                if name and url:
                    functions[name.group(1)] = (url.group(1).upper(), url.group(2))
    return functions


# --------------------------------------------------------------------------- matching


def call_evidence(op: Operation, usage: FrontendUsage, sdk_functions: dict[str, tuple[str, str]]) -> str | None:
    """The strongest kind of evidence (`sdk`/`fetch`/`url`/`url-sfx`) that `op` is called, or None."""
    full = normalize_path(op.path)
    relative = full[len(op.mount) :] if op.mount and full.startswith(op.mount) else full
    candidates = {full, relative}

    sdk_names = {fn for fn, (method, url) in sdk_functions.items() if method == op.method and normalize_path(url) in candidates}
    for fn in sdk_names:
        if fn in usage.identifiers or any(fn + suffix in usage.identifiers for suffix in _REACT_QUERY_SUFFIXES):
            return "sdk"
    if any((op.method, c) in usage.fetch_calls for c in candidates):
        return "fetch"
    if candidates & usage.literals:
        return "url"
    for literal in usage.literals:
        bare = re.sub(r"^(\{\})+", "", literal)
        if bare.count("/") < 2 or bare == "/":
            continue
        if any(c == bare or c.endswith(bare) or (len(c) > 3 and bare.endswith(c)) for c in candidates) and "{}" not in bare.split("/")[1:2]:
            return "url-sfx"
    return None


def is_excluded(
    op: Operation, *, prefixes: tuple[str, ...] | list[str], tags: tuple[str, ...] | list[str], path_globs: tuple[str, ...] | list[str]
) -> bool:
    return (
        any(op.path.startswith(p) for p in prefixes)
        or any(t in tags for t in op.tags)
        or any(fnmatch(op.path, g) or fnmatch(op.key, g) for g in path_globs)
    )


def find_uncalled(
    operations: list[Operation],
    usage: FrontendUsage,
    sdk_functions: dict[str, tuple[str, str]],
    *,
    exclude_prefixes: tuple[str, ...] | list[str] = (),
    exclude_tags: tuple[str, ...] | list[str] = (),
    exclude_paths: tuple[str, ...] | list[str] = (),
) -> list[Operation]:
    return [
        op
        for op in operations
        if not is_excluded(op, prefixes=exclude_prefixes, tags=exclude_tags, path_globs=exclude_paths)
        and call_evidence(op, usage, sdk_functions) is None
    ]


# --------------------------------------------------------------------------- orchestration


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
    baseline: str | None = None


def parse_config(table: dict) -> list[AppConfig]:
    apps = table.get("apps") or []
    if not apps:
        raise ApiUsageError("no [[tool.bdt.api_usage.apps]] configured in pyproject.toml")
    configs: list[AppConfig] = []
    for i, raw in enumerate(apps):
        name = str(raw.get("name") or raw.get("app") or raw.get("openapi") or f"app{i}")
        if bool(raw.get("app")) == bool(raw.get("openapi")):
            raise ApiUsageError(f"api_usage app '{name}': set exactly one of `app` (module:attr) and `openapi` (path to openapi.json)")
        if not raw.get("frontends"):
            raise ApiUsageError(f"api_usage app '{name}': `frontends` (list of frontend source dirs/globs) is required")
        configs.append(
            AppConfig(
                name=name,
                frontends=[str(f) for f in raw["frontends"]],
                app=raw.get("app"),
                openapi=raw.get("openapi"),
                app_dir=str(raw.get("app_dir", ".")),
                env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
                exclude_prefixes=[str(p) for p in raw.get("exclude_prefixes", [])],
                exclude_tags=[str(t) for t in raw.get("exclude_tags", [])],
                exclude_paths=[str(p) for p in raw.get("exclude_paths", [])],
                exclude_frontend_globs=[str(g) for g in raw.get("exclude_frontend_globs", [])],
                baseline=raw.get("baseline"),
            )
        )
    return configs


def _resolve_frontend_dirs(repo_root: Path, patterns: list[str]) -> list[Path]:
    dirs: list[Path] = []
    for pattern in patterns:
        matches = sorted(p for p in repo_root.glob(pattern) if p.is_dir())
        if not matches:
            raise ApiUsageError(f"frontends entry '{pattern}' matches no directory under {repo_root}")
        dirs.extend(matches)
    return dirs


def read_baseline(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")}


def write_baseline(path: Path, keys: list[str]) -> None:
    header = "# Backend operations known not to be called from the frontend (bdt lint-api-usage). Remove a line once the route is deleted or called.\n"
    path.write_text(header + "".join(f"{k}\n" for k in sorted(keys)), encoding="utf-8")


def check_app(config: AppConfig, *, repo_root: Path, update_baseline: bool = False) -> list[Finding]:
    if config.openapi:
        operations = load_openapi_file(repo_root / config.openapi)
    else:
        assert config.app is not None
        operations = load_app_operations(config.app, cwd=repo_root / config.app_dir, env=config.env)

    frontend_dirs = _resolve_frontend_dirs(repo_root, config.frontends)
    usage = scan_frontend(iter_frontend_files(frontend_dirs, repo_root=repo_root, exclude_globs=config.exclude_frontend_globs))
    uncalled = find_uncalled(
        operations,
        usage,
        read_sdk_functions(frontend_dirs),
        exclude_prefixes=config.exclude_prefixes,
        exclude_tags=config.exclude_tags,
        exclude_paths=config.exclude_paths,
    )

    keys = [op.key for op in uncalled]
    where = Path(config.name)
    if config.baseline:
        baseline_path = repo_root / config.baseline
        if update_baseline:
            write_baseline(baseline_path, keys)
            return []
        baseline = read_baseline(baseline_path)
        by_key = {op.key: op for op in uncalled}
        findings = [_finding(where, op) for key, op in by_key.items() if key not in baseline]
        known = {op.key for op in operations}
        findings.extend(
            Finding(
                where,
                0,
                RULE_STALE_BASELINE,
                f"'{key}' is listed in {config.baseline} but is "
                + ("now called from the frontend" if key in known else "no longer a backend route")
                + " -- remove it (or run `bdt lint-api-usage --update-baseline`).",
            )
            for key in sorted(baseline - set(by_key))
        )
        return findings
    if update_baseline:
        raise ApiUsageError(f"api_usage app '{config.name}' has no `baseline` configured, nothing to update")
    return [_finding(where, op) for op in uncalled]


def _finding(where: Path, op: Operation) -> Finding:
    tags = f" [{', '.join(op.tags)}]" if op.tags else ""
    return Finding(
        where,
        0,
        RULE,
        f"{op.key}{tags} is never called from non-generated frontend code -- delete it, or exclude it "
        "(exclude_prefixes/exclude_tags/exclude_paths) if something other than the frontend calls it.",
    )


def run(table: dict, *, repo_root: Path, update_baseline: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    for config in parse_config(table):
        findings.extend(check_app(config, repo_root=repo_root, update_baseline=update_baseline))
    return findings
