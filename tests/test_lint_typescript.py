import json
from pathlib import Path

from bmsdna.devtools import lint
from bmsdna.devtools.lint_typescript import (
    RULE_HTTP,
    RULE_MODEL,
    check_typescript_file,
    find_generator,
    is_excluded_ts_file,
)


def _check(source: str) -> list[tuple[int, str]]:
    findings = check_typescript_file(Path("x.ts"), generator="openapi-fetch", source=source)
    return [(f.line, f.rule) for f in findings]


def test_plain_json_fetch_is_flagged() -> None:
    src = 'export async function load() {\n  const res = await fetch("/api/things");\n  return res.json();\n}\n'
    assert _check(src) == [(2, RULE_HTTP)]


def test_fetch_with_template_url_and_post_json_is_flagged() -> None:
    src = "const r = await fetch(`/api/items/${id}`, {\n  method: 'POST',\n  body: JSON.stringify(x),\n});\n"
    assert _check(src) == [(1, RULE_HTTP)]


def test_inline_then_json_fetch_is_flagged() -> None:
    assert _check('const q = { queryFn: () => fetch("/api/me").then((r) => r.json()) };\n') == [(1, RULE_HTTP)]


def test_axios_calls_and_instances_are_flagged() -> None:
    src = "axios.get<Foo>('/api/foo');\nconst c = axios.create({ baseURL: '/api' });\naxios({ url: '/x' });\n"
    assert _check(src) == [(1, RULE_HTTP), (2, RULE_HTTP), (3, RULE_HTTP)]


def test_xhr_is_flagged() -> None:
    assert _check("const x = new XMLHttpRequest();\n") == [(1, RULE_HTTP)]


def test_window_fetch_is_flagged() -> None:
    assert _check("await window.fetch('/api/a');\n") == [(1, RULE_HTTP)]


def test_method_named_fetch_and_other_identifiers_are_not_flagged() -> None:
    src = "client.fetch('/x');\nprefetch('/x');\nuseFetch('/x');\nconst fetchAll = 1;\n"
    assert _check(src) == []


def test_comments_and_strings_are_ignored() -> None:
    src = (
        "// fetch('/api/a')\n"
        "/* fetch('/api/b')\n   spanning */\n"
        "const s = \"fetch('/api/c')\";\n"
        "const t = `use fetch(x) here`;\n"
        "/**\n * fetch (the request) docs\n */\n"
    )
    assert _check(src) == []


def test_fetch_inside_template_expression_is_flagged() -> None:
    assert _check("const t = `${await fetch('/api/a')}`;\n") == [(1, RULE_HTTP)]


def test_apostrophe_in_jsx_text_does_not_hide_later_calls() -> None:
    src = "const a = <p>Don't</p>;\nawait fetch('/api/a');\n"
    assert _check(src) == [(2, RULE_HTTP)]


def test_external_url_is_not_flagged() -> None:
    src = 'await fetch("https://nominatim.openstreetmap.org/search");\nawait fetch(`//cdn.example.com/x.json`);\n'
    assert _check(src) == []


def test_external_url_held_in_a_variable_is_not_flagged() -> None:
    src = (
        "async function geocode(q: string) {\n"
        "  const url =\n"
        "    'https://api3.geo.admin.ch/rest/x' +\n"
        "    `?q=${q}`;\n"
        "  const res = await fetch(url, { signal });\n"
        "}\n"
    )
    assert _check(src) == []


def test_text_only_response_is_not_flagged_but_error_text_alone_does_not_exempt() -> None:
    text_only = "async function md() {\n  const r = await fetch('/api/r.md');\n  return r.text();\n}\n"
    err_text = "async function a() {\n  const r = await fetch('/api/a');\n  if (!r.ok) throw new Error(await r.text());\n  return r.json();\n}\n"
    assert _check(text_only) == []
    assert _check(err_text) == [(2, RULE_HTTP)]
    mutation = "async function a() {\n  const r = await fetch('/api/a', { method: 'POST' });\n  if (!r.ok) throw new Error(await r.text());\n}\n"
    assert _check(mutation) == [(2, RULE_HTTP)]


def test_sse_helper_is_not_flagged() -> None:
    src = "async function open(url: string) {\n  const response = await fetch(url);\n  if (!response.body) throw new Error('x');\n}\n"
    assert _check(src) == []


def test_formdata_upload_is_not_flagged() -> None:
    src = (
        "async function upload(file: File) {\n"
        "  const body = new FormData();\n"
        "  body.append('file', file);\n"
        "  const res = await fetch('/api/upload', { method: 'POST', body });\n"
        "  return res.json();\n"
        "}\n"
    )
    assert _check(src) == []


def test_blob_download_is_not_flagged() -> None:
    src = "async function dl() {\n  const res = await fetch('/api/export.xlsx');\n  return await res.blob();\n}\n"
    assert _check(src) == []


def test_sse_stream_is_not_flagged() -> None:
    src = (
        "async function stream() {\n"
        "  const res = await fetch('/api/chat', { method: 'POST', body: '{}' });\n"
        "  const reader = res.body!.getReader();\n"
        "}\n"
    )
    assert _check(src) == []


def test_marker_in_a_different_function_does_not_exempt() -> None:
    src = (
        "async function a() {\n"
        "  const res = await fetch('/api/a');\n"
        "  return res.json();\n"
        "}\n"
        "async function b() {\n"
        "  const body = new FormData();\n"
        "}\n"
    )
    assert _check(src) == [(2, RULE_HTTP)]


def test_marker_in_a_comment_does_not_exempt() -> None:
    src = "async function a() {\n  // not a FormData upload\n  const res = await fetch('/api/a');\n}\n"
    assert _check(src) == [(3, RULE_HTTP)]


def test_ignore_pragma_same_line_and_previous_line() -> None:
    src = (
        "await fetch('/api/a'); // bdt-lint: ignore ts-handwired-http -- legacy\n"
        "// bdt-lint: ignore ts-handwired-http, ts-handwired-model -- legacy\n"
        "await fetch('/api/b');\n"
        "await fetch('/api/c'); // bdt-lint: ignore ts-handwired-model\n"
    )
    assert _check(src) == [(4, RULE_HTTP)]


def test_json_cast_outside_a_fetch_is_a_model_finding() -> None:
    src = "function parse(res: Response) {\n  return res.json() as Promise<Foo>;\n}\n"
    assert _check(src) == [(2, RULE_MODEL)]


def test_annotated_json_result_is_a_model_finding() -> None:
    assert _check("const data: Foo = await res.json();\n") == [(1, RULE_MODEL)]


def test_json_cast_to_unknown_is_not_a_model_finding() -> None:
    assert _check("const d = (await res.json()) as unknown;\n") == []


def test_json_cast_inside_flagged_fetch_is_reported_once() -> None:
    src = "async function api<T>(url: string) {\n  const res = await fetch(url);\n  return res.json() as Promise<T>;\n}\n"
    assert _check(src) == [(2, RULE_HTTP)]


def test_generated_and_test_files_are_excluded(tmp_path: Path) -> None:
    for rel in (
        "src/lib/client.gen.ts",
        "src/lib/openapi_schema.generated.ts",
        "src/lib/generated/index.ts",
        "src/lib/api-types.ts",
        "src/types.d.ts",
        "src/a.test.tsx",
        "src/a.spec.ts",
        "src/__tests__/a.ts",
        "e2e/a.ts",
    ):
        assert is_excluded_ts_file(tmp_path / rel, repo_root=tmp_path), rel
    assert not is_excluded_ts_file(tmp_path / "src/lib/api.ts", repo_root=tmp_path)
    assert is_excluded_ts_file(tmp_path / "src/legacy/old.ts", repo_root=tmp_path, exclude_globs=["src/legacy/*"])


def test_find_generator_uses_nearest_package_json(tmp_path: Path) -> None:
    (tmp_path / "with_gen/src").mkdir(parents=True)
    (tmp_path / "with_gen/package.json").write_text(json.dumps({"devDependencies": {"openapi-typescript": "^7"}}))
    (tmp_path / "no_gen/src").mkdir(parents=True)
    (tmp_path / "no_gen/package.json").write_text(json.dumps({"dependencies": {"react": "^19"}}))
    (tmp_path / "script_gen/src").mkdir(parents=True)
    (tmp_path / "script_gen/package.json").write_text(json.dumps({"scripts": {"gen": "openapi-ts -i x.json"}}))
    cache: dict[Path, str | None] = {}
    assert find_generator(tmp_path / "with_gen/src/a.ts", repo_root=tmp_path, cache=cache) == "openapi-typescript"
    assert find_generator(tmp_path / "no_gen/src/a.ts", repo_root=tmp_path, cache=cache) is None
    assert find_generator(tmp_path / "script_gen/src/a.ts", repo_root=tmp_path, cache=cache) == "an openapi script"
    assert find_generator(tmp_path / "orphan/a.ts", repo_root=tmp_path, cache=cache) is None


def test_run_flags_only_packages_with_a_generator(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    for pkg, deps in (("withgen", {"openapi-fetch": "^0.15"}), ("plain", {"react": "^19"})):
        (tmp_path / pkg / "src").mkdir(parents=True)
        (tmp_path / pkg / "package.json").write_text(json.dumps({"dependencies": deps}))
        (tmp_path / pkg / "src/api.ts").write_text("export const me = () => fetch('/api/me').then((r) => r.json());\n")
    (tmp_path / "withgen/node_modules/dep").mkdir(parents=True)
    (tmp_path / "withgen/node_modules/dep/index.ts").write_text("fetch('/x');\n")

    result = lint.run([], root=tmp_path, skip_tooling_check=True)

    assert [(f.path.relative_to(tmp_path).as_posix(), f.rule) for f in result.findings] == [("withgen/src/api.ts", RULE_HTTP)]


def test_run_honours_ts_exclude_globs_and_extra_markers(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\n[tool.bdt.lint]\nts_exclude_globs=['src/legacy/*']\nts_non_json_markers=['WebSocket']\n"
    )
    (tmp_path / "src/legacy").mkdir(parents=True)
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"orval": "^7"}}))
    (tmp_path / "src/legacy/old.ts").write_text("fetch('/a');\n")
    (tmp_path / "src/ws.ts").write_text("function f() {\n  const w = new WebSocket('/ws');\n  w.send(1);\n  fetch('/a');\n}\n")

    assert lint.run([], root=tmp_path, skip_tooling_check=True).ok


def test_run_scans_explicit_ts_file(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"openapi-fetch": "^0.15"}}))
    target = tmp_path / "a.tsx"
    target.write_text("fetch('/api/a');\n")

    result = lint.run([str(target)], root=tmp_path, skip_tooling_check=True)

    assert [f.rule for f in result.findings] == [RULE_HTTP]


def test_alphanumeric_markers_match_whole_words_only() -> None:
    src = "async function a() {\n  const ASSET = 1;\n  const res = await fetch('/api/a');\n  return res.json();\n}\n"
    assert _check(src) == [(3, RULE_HTTP)]


def test_declarations_named_fetch_are_not_calls() -> None:
    src = "class Repo {\n  async fetch(id: string) {\n    return id;\n  }\n}\nfunction fetch(u: string) {\n  return u;\n}\n"
    assert _check(src) == []


def test_trailing_pragma_only_covers_its_own_line() -> None:
    src = "await fetch('/a'); // bdt-lint: ignore ts-handwired-http -- x\nawait fetch('/b');\n"
    assert _check(src) == [(2, RULE_HTTP)]


def test_nested_axios_generics_are_flagged() -> None:
    assert _check("axios.get<Array<Foo>>('/a');\n") == [(1, RULE_HTTP)]


def test_incoming_request_body_cast_is_not_a_model_finding() -> None:
    assert _check("const body = (await request.json()) as CreateBody;\nconst b2 = (await req.json()) as X;\n") == []


def test_fetch_wrapper_passed_to_generated_client_is_not_flagged() -> None:
    src = "export const client = createClient<paths>({\n  baseUrl: '/api',\n  fetch: (req) => fetch(req, { credentials: 'include' }),\n});\n"
    assert _check(src) == []


def test_mts_test_and_declaration_files_are_excluded(tmp_path: Path) -> None:
    for rel in ("a.spec.mts", "a.test.mts", "types.d.mts", "a.gen.tsx"):
        assert is_excluded_ts_file(tmp_path / rel, repo_root=tmp_path), rel


def test_marker_only_package_json_defers_to_parent(tmp_path: Path) -> None:
    (tmp_path / "src/legacy").mkdir(parents=True)
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"openapi-fetch": "^0.15"}}))
    (tmp_path / "src/legacy/package.json").write_text(json.dumps({"type": "module"}))
    assert find_generator(tmp_path / "src/legacy/a.ts", repo_root=tmp_path, cache={}) == "openapi-fetch"


def test_sse_stream_markers_are_not_flagged() -> None:
    for marker in ("EventSource", "getReader(", "text/event-stream", "SSE"):
        src = f"async function s() {{\n  // {marker}\n  const r = await fetch('/api/s', {{ headers: {{ a: '{marker}' }} }});\n}}\n"
        assert _check(src) == [], marker
