import json
import textwrap
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bmsdna.devtools import _api_dump
from bmsdna.devtools import api_usage as au
from bmsdna.devtools.bdt_config import load_bdt_table
from bmsdna.devtools.cli import app

_SDK = """
export const getThing = <T extends boolean = false>(options: Options<GetThingData, T>) =>
  (options.client ?? client).get<GetThingResponses, GetThingErrors, T>({
    url: "/api/things/{thing_id}",
    ...options,
  });

export const deleteThing = <T extends boolean = false>(options: Options<DeleteThingData, T>) =>
  (options.client ?? client).delete<DeleteThingResponses, DeleteThingErrors, T>({
    url: "/api/things/{thing_id}",
    ...options,
  });

export const listThings = (options?: Options<ListThingsData>) =>
  (options?.client ?? client).get<ListThingsResponses, ListThingsErrors>({ url: "/api/things", ...options });

export const headThings = (options?: Options<HeadThingsData>) =>
  (options?.client ?? client).head<HeadThingsResponses, HeadThingsErrors>({ url: "/api/things", ...options });
"""


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def _fe(
    root: Path, rel: str = "fe/src", *, exclude_globs: list[str] | None = None
) -> au.Frontend:
    return au.build_frontend(
        root / rel, repo_root=root, exclude_globs=exclude_globs or []
    )


def _op(method: str, path: str, *tags: str, mount: str = "") -> au.Operation:
    return au.Operation(method, path, tuple(tags), mount)


def _evidence(op: au.Operation, *frontends: au.Frontend) -> str | None:
    return au.call_evidence(op, list(frontends))


# ------------------------------------------------------------------ path normalisation / template scanning


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/api/x/{id}/y", "/api/x/{}/y"),
        ("/api/x/${id}/y?a=${b}", "/api/x/{}/y"),
        ("/api/x/{}{}", "/api/x/{}"),
        ("/api/x${qs}", "/api/x"),  # glued trailing expression is a query suffix
        ("/api/x/${id}", "/api/x/{}"),  # but a placeholder segment is a path parameter
        ("/api/x/", "/api/x"),
    ],
)
def test_normalize_path(raw: str, expected: str) -> None:
    assert au.normalize_path(raw) == expected


def test_read_template_handles_nested_template_in_expression() -> None:
    src = '`/api/m/${encodeURIComponent(id)}/att${mailbox ? `?mb=${encodeURIComponent(mailbox)}` : ""}` + rest'
    text, end = au._read_template(src, 0, len(src))
    assert text == "/api/m/{}/att{}"
    assert src[end:] == " + rest"


def test_hostile_input_is_bounded(tmp_path: Path) -> None:
    deep = "`/${" * 5000  # would recurse ~5000 levels
    lone = "'${" * 70_000  # quadratic if the optional group may run to end of file
    unclosed = "`/${" * 300 + "x" * 200_000
    sdk_like = "export const x = " + ".get " * 30_000
    _write(tmp_path, "fe/src/a.ts", deep + "\n" + lone + "\n" + unclosed + "\n")
    _write(tmp_path, "fe/src/lib/generated/sdk.gen.ts", sdk_like)
    started = time.monotonic()
    fe = _fe(tmp_path)
    assert time.monotonic() - started < 5
    assert fe.usage.files == 1


# ------------------------------------------------------------------ backend inventory


def test_ops_from_doc_prefixes_mount_and_keeps_tags() -> None:
    doc = {"paths": {"/a/{id}": {"get": {"tags": ["t"]}, "post": {}, "parameters": []}}}
    ops = [au.Operation.from_dict(d) for d in _api_dump.ops_from_doc(doc, "/api/sub")]
    assert {(o.method, o.path, o.tags, o.mount) for o in ops} == {
        ("GET", "/api/sub/a/{id}", ("t",), "/api/sub"),
        ("POST", "/api/sub/a/{id}", (), "/api/sub"),
    }


class _FakeMount:
    def __init__(self, path: str, app: object) -> None:
        self.path, self.app = path, app


class _FakeApp:
    def __init__(self, paths: dict, routes: list | None = None) -> None:
        self._paths, self.routes = paths, routes or []

    def openapi(self) -> dict:
        return {"paths": self._paths}


def test_collect_recurses_into_mounts_only() -> None:
    sub = _FakeApp({"/items": {"get": {}}})
    static = object()  # a StaticFiles-like mount: no openapi()
    root = _FakeApp(
        {"/health": {"get": {}}},
        [_FakeMount("/api/sub/", sub), _FakeMount("/assets", static), object()],
    )
    assert sorted(f"{o['method']} {o['path']}" for o in _api_dump.collect(root)) == [
        "GET /api/sub/items",
        "GET /health",
    ]


def test_load_app_operations_runs_in_subprocess_with_cwd_on_path(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "svc/fakeapp.py",
        """
        class A:
            routes = []
            def openapi(self):
                return {"paths": {"/api/ping": {"get": {"tags": ["x"]}}}}
        app = A()
        """,
    )
    ops = au.load_app_operations("fakeapp:app", cwd=tmp_path / "svc")
    assert [(o.key, o.tags) for o in ops] == [("GET /api/ping", ("x",))]


def test_load_app_operations_survives_repo_local_bmsdna_package(tmp_path: Path) -> None:
    # CCMT2 ships its own top-level `bmsdna` package, which the app itself imports; it must neither break the
    # dump (by shadowing bmsdna.devtools) nor be shadowed by it.
    _write(tmp_path, "bmsdna/__init__.py")
    _write(tmp_path, "bmsdna/links.py", "PREFIX = '/api/linked'\n")
    _write(
        tmp_path,
        "linkedapp.py",
        """
        from bmsdna.links import PREFIX
        class A:
            routes = []
            def openapi(self):
                return {"paths": {PREFIX: {"get": {}}}}
        app = A()
        """,
    )
    assert [o.key for o in au.load_app_operations("linkedapp:app", cwd=tmp_path)] == [
        "GET /api/linked"
    ]


def test_load_app_operations_reports_import_errors(tmp_path: Path) -> None:
    _write(tmp_path, "boom.py", "raise RuntimeError('missing env var FOO')\n")
    with pytest.raises(au.ApiUsageError, match="missing env var FOO"):
        au.load_app_operations("boom:app", cwd=tmp_path)


def test_load_app_operations_rejects_bad_spec_and_dir(tmp_path: Path) -> None:
    with pytest.raises(au.ApiUsageError, match="module.path:attribute"):
        au.load_app_operations("nocolon", cwd=tmp_path)
    with pytest.raises(au.ApiUsageError, match="not a directory"):
        au.load_app_operations("a:b", cwd=tmp_path / "missing")


def test_load_app_operations_passes_env(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "envapp.py",
        """
        import os
        class A:
            routes = []
            def openapi(self):
                return {"paths": {"/" + os.environ["ROUTE_NAME"]: {"get": {}}}}
        app = A()
        """,
    )
    assert [
        o.path
        for o in au.load_app_operations(
            "envapp:app", cwd=tmp_path, env={"ROUTE_NAME": "hello"}
        )
    ] == ["/hello"]


def test_load_openapi_file_rejects_non_object(tmp_path: Path) -> None:
    _write(tmp_path, "o.json", "[]")
    with pytest.raises(au.ApiUsageError, match="not a JSON object"):
        au.load_openapi_file(tmp_path / "o.json")


# ------------------------------------------------------------------ frontend evidence


def test_sdk_function_and_react_query_helper_count_as_calls(tmp_path: Path) -> None:
    _write(tmp_path, "fe/src/lib/generated/sdk.gen.ts", _SDK)
    _write(
        tmp_path,
        "fe/src/page.tsx",
        "import { listThingsOptions } from '@/lib/generated/@tanstack/react-query.gen';\nuseQuery(listThingsOptions());\n",
    )
    _write(
        tmp_path,
        "fe/src/other.ts",
        "import { getThing } from './lib/generated';\ngetThing({ path: { thing_id: 1 } });\n",
    )
    fe = _fe(tmp_path)
    assert set(fe.sdk) == {"getThing", "deleteThing", "listThings", "headThings"}
    assert _evidence(_op("GET", "/api/things"), fe) == "sdk"
    assert _evidence(_op("GET", "/api/things/{thing_id}"), fe) == "sdk"
    # same path, different method: deleteThing / headThings are never referenced
    assert _evidence(_op("DELETE", "/api/things/{thing_id}"), fe) is None
    assert _evidence(_op("HEAD", "/api/things"), fe) is None


def test_react_query_suffix_does_not_confuse_two_sdk_functions(tmp_path: Path) -> None:
    sdk = """
    export const getUser = (o) => (o.client ?? client).get({ url: "/api/user", ...o });
    export const getUserQuery = (o) => (o.client ?? client).get({ url: "/api/user-query", ...o });
    """
    _write(tmp_path, "fe/src/lib/generated/sdk.gen.ts", sdk)
    _write(tmp_path, "fe/src/p.ts", "getUserQuery();\n")
    fe = _fe(tmp_path)
    assert _evidence(_op("GET", "/api/user-query"), fe) == "sdk"
    assert _evidence(_op("GET", "/api/user"), fe) is None


def test_sdk_functions_are_scoped_per_frontend(tmp_path: Path) -> None:
    a_sdk = 'export const listItems = (o) => (o.client ?? client).get({ url: "/api/a/items", ...o });\n'
    b_sdk = 'export const listItems = (o) => (o.client ?? client).get({ url: "/api/b/items", ...o });\n'
    _write(tmp_path, "apps/a/src/gen/sdk.gen.ts", a_sdk)
    _write(tmp_path, "apps/a/src/page.ts", "listItems();\n")
    _write(tmp_path, "apps/b/src/gen/sdk.gen.ts", b_sdk)
    _write(tmp_path, "apps/b/src/page.ts", "const x = 1;\n")
    a, b = _fe(tmp_path, "apps/a/src"), _fe(tmp_path, "apps/b/src")
    assert _evidence(_op("GET", "/api/a/items"), a, b) == "sdk"
    assert (
        _evidence(_op("GET", "/api/b/items"), a, b) is None
    )  # app b generates listItems too, but never calls it


@pytest.mark.parametrize(
    "name",
    [
        "lib/generated/react-query.gen.ts",
        "lib/api-types.generated.ts",
        "lib/api-types-v2.ts",
        "lib/openapi_schema.generated.ts",
        "lib/types.d.ts",
        "page.test.tsx",
        "page.test.js",
        "page.spec.jsx",
        "client.gen.js",
        "client.generated.mjs",
        "Page.test.vue",
        "__tests__/a.ts",
        "e2e/a.ts",
        "tests/a.js",
    ],
)
def test_generated_code_and_tests_are_not_callers(tmp_path: Path, name: str) -> None:
    _write(
        tmp_path,
        f"fe/src/{name}",
        'fetch("/api/things/1"); export const x = deleteThing(); const p = "/api/things/{thing_id}";\n',
    )
    _write(tmp_path, "fe/src/lib/generated/sdk.gen.ts", _SDK)
    fe = _fe(tmp_path)
    assert fe.usage.files == 0
    assert _evidence(_op("DELETE", "/api/things/{thing_id}"), fe) is None
    assert _evidence(_op("GET", "/api/things/{thing_id}"), fe) is None


def test_comments_are_not_calls(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "fe/src/a.ts",
        """
        // fetch("/api/commented")
        /* client.GET("/api/block") */
        const label = "it's";
        const jsx = <p>Don't</p>;
        fetch("/api/real");
        """,
    )
    fe = _fe(tmp_path)
    assert _evidence(_op("GET", "/api/real"), fe) == "url"
    assert _evidence(_op("GET", "/api/commented"), fe) is None
    assert _evidence(_op("GET", "/api/block"), fe) is None


def test_openapi_fetch_literal_is_method_exact(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "fe/src/svc.ts",
        """
        const { data } = await client.GET("/api/things/{thing_id}", { params: { path: { thing_id } } });
        await client
          .POST(
            "/api/things",
            { body },
          );
        """,
    )
    fe = _fe(tmp_path)
    assert _evidence(_op("GET", "/api/things/{thing_id}"), fe) == "fetch"
    assert _evidence(_op("POST", "/api/things"), fe) == "fetch"
    assert _evidence(_op("DELETE", "/api/things/{thing_id}"), fe) is None
    assert _evidence(_op("GET", "/api/things"), fe) is None


def test_handwritten_fetch_is_method_agnostic(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "fe/src/svc.ts",
        "await fetch(`/api/things/${id}`, { method: 'DELETE' });\n",
    )
    fe = _fe(tmp_path)
    for method in ("GET", "DELETE"):
        assert _evidence(_op(method, "/api/things/{thing_id}"), fe) == "url"


def test_handwritten_fetch_with_nested_template(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "fe/src/mail.ts",
        """
        const url = `/api/offer-parser/mail/${encodeURIComponent(mail.id)}/attachment${mailbox ? `?mailbox=${encodeURIComponent(mailbox)}` : ""}`;
        """,
    )
    assert (
        _evidence(
            _op("GET", "/api/offer-parser/mail/{message_id}/attachment"), _fe(tmp_path)
        )
        == "url"
    )


def test_concatenated_and_multi_expression_urls(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "fe/src/c.ts",
        """
        await fetch("/api/items/" + id);
        await fetch("/api/items/" + id + "/details");
        await fetch(`${a}${b}/api/viaprefix/${id}`);
        """,
    )
    fe = _fe(tmp_path)
    assert _evidence(_op("GET", "/api/items/{item_id}"), fe) == "url"
    assert (
        _evidence(_op("GET", "/api/viaprefix/{x}"), fe) == "url-sfx"
    )  # leading base-URL expressions are ignored


def test_suffix_match_for_base_url_relative_clients(tmp_path: Path) -> None:
    _write(
        tmp_path, "fe/src/c.ts", "const r = await http.get(`/customers/${id}/sales`);\n"
    )
    fe = _fe(tmp_path)
    assert _evidence(_op("GET", "/api/customers/{customer_id}/sales"), fe) == "url-sfx"
    assert _evidence(_op("GET", "/api/customers/{customer_id}/other"), fe) is None


def test_literal_longer_than_route_is_not_evidence(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "fe/src/c.ts",
        'router.push("/admin/items/list");\nconst root = "/";\nsplit("/");\n',
    )
    fe = _fe(tmp_path)
    assert _evidence(_op("GET", "/items/list"), fe) is None
    assert _evidence(_op("GET", "/"), fe) is None


def test_mount_prefix_is_stripped_for_sdk_urls(tmp_path: Path) -> None:
    _write(
        tmp_path, "fe/src/gen/sdk.gen.ts", _SDK.replace('"/api/things"', '"/things"')
    )
    _write(tmp_path, "fe/src/p.ts", "listThings();\n")
    fe = _fe(tmp_path)
    # a sub-app's generated client uses mount-relative urls ("/things"); the operation carries the mount ("/api/sub/things")
    assert _evidence(_op("GET", "/api/sub/things", mount="/api/sub"), fe) == "sdk"
    assert _evidence(_op("GET", "/things"), fe) == "sdk"
    assert _evidence(_op("GET", "/api/sub/other", mount="/api/sub"), fe) is None
    assert (
        _evidence(_op("GET", "/api/sub/", mount="/api/sub"), fe) is None
    )  # mount root must not crash on the empty remainder


# ------------------------------------------------------------------ excludes


def test_is_excluded_applies_prefix_tag_and_glob() -> None:
    kwargs = {
        "prefixes": ["/external_api"],
        "tags": ["agent"],
        "path_globs": ["/auth/*", "POST /api/svc/*"],
    }
    assert au.is_excluded(_op("GET", "/external_api/a"), **kwargs)
    assert au.is_excluded(_op("GET", "/api/agent-tool", "agent"), **kwargs)
    assert au.is_excluded(_op("GET", "/auth/callback"), **kwargs)
    assert au.is_excluded(_op("POST", "/api/svc/sync"), **kwargs)
    assert not au.is_excluded(_op("GET", "/api/svc/sync"), **kwargs)
    assert not au.is_excluded(_op("GET", "/api/dead"), **kwargs)


# ------------------------------------------------------------------ config / orchestration


def test_parse_config_validation() -> None:
    with pytest.raises(au.ApiUsageError, match="no .*apps"):
        au.parse_config({})
    with pytest.raises(au.ApiUsageError, match="exactly one"):
        au.parse_config({"apps": [{"name": "x", "frontends": ["fe"]}]})
    with pytest.raises(au.ApiUsageError, match="exactly one"):
        au.parse_config(
            {
                "apps": [
                    {
                        "name": "x",
                        "app": "a:b",
                        "openapi": "o.json",
                        "frontends": ["fe"],
                    }
                ]
            }
        )
    with pytest.raises(au.ApiUsageError, match="frontends"):
        au.parse_config({"apps": [{"name": "x", "app": "a:b"}]})


@pytest.mark.parametrize(
    "key",
    [
        "frontends",
        "exclude_prefixes",
        "exclude_tags",
        "exclude_paths",
        "exclude_frontend_globs",
    ],
)
def test_parse_config_rejects_strings_where_lists_are_required(key: str) -> None:
    raw = {"name": "x", "app": "a:b", "frontends": ["fe"], key: "/internal"}
    with pytest.raises(au.ApiUsageError, match=f"`{key}` must be a list of strings"):
        au.parse_config({"apps": [raw]})


def _project(root: Path, *, baseline: bool = False, extra: str = "") -> None:
    _write(
        root,
        "openapi.json",
        json.dumps(
            {
                "paths": {
                    "/api/used": {"get": {}},
                    "/api/dead": {"get": {"tags": ["t"]}},
                    "/external/x": {"get": {}},
                }
            }
        ),
    )
    _write(root, "fe/src/a.ts", "fetch('/api/used');\n")
    baseline_line = 'baseline = "api-usage-baseline.txt"\n' if baseline else ""
    _write(
        root,
        "pyproject.toml",
        f"""
        [project]
        name = "x"

        [[tool.bdt.dead_code.apps]]
        name = "main"
        openapi = "openapi.json"
        frontends = ["fe/src"]
        exclude_prefixes = ["/external"]
        {baseline_line}{extra}
        """,
    )


def _config(root: Path) -> au.AppConfig:
    return au.parse_config(load_bdt_table("dead_code", root))[0]


def test_check_app_reports_uncalled_routes(tmp_path: Path) -> None:
    _project(tmp_path)
    findings = au.check_app(_config(tmp_path), repo_root=tmp_path)
    assert [(f.rule, f.message.split(" is never")[0]) for f in findings] == [
        ("api-route-uncalled", "GET /api/dead [t]")
    ]


def test_frontend_globs_expand_and_exclude_frontend_globs_apply(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "openapi.json",
        json.dumps(
            {
                "paths": {
                    "/api/a": {"get": {}},
                    "/api/b": {"get": {}},
                    "/api/c": {"get": {}},
                }
            }
        ),
    )
    _write(tmp_path, "apps/one/src/x.ts", "fetch('/api/a');\n")
    _write(tmp_path, "apps/two/src/x.ts", "fetch('/api/b');\n")
    _write(tmp_path, "apps/two/src/legacy/old.ts", "fetch('/api/c');\n")
    config = au.AppConfig(
        name="m",
        openapi="openapi.json",
        frontends=["apps/*/src"],
        exclude_frontend_globs=["apps/two/src/legacy/*"],
    )
    assert [
        f.message.split(" ")[1] for f in au.check_app(config, repo_root=tmp_path)
    ] == ["/api/c"]


def test_frontends_entry_must_match_and_be_relative(tmp_path: Path) -> None:
    _project(tmp_path)
    with pytest.raises(au.ApiUsageError, match="matches no directory"):
        au.check_app(
            au.AppConfig(name="m", openapi="openapi.json", frontends=["nope/*/src"]),
            repo_root=tmp_path,
        )
    with pytest.raises(au.ApiUsageError, match="repo-relative"):
        au.check_app(
            au.AppConfig(name="m", openapi="openapi.json", frontends=["/abs"]),
            repo_root=tmp_path,
        )


def test_empty_inventory_is_an_error_not_a_clean_pass(tmp_path: Path) -> None:
    _project(tmp_path)
    _write(tmp_path, "openapi.json", "{}")
    with pytest.raises(au.ApiUsageError, match="no operations found"):
        au.check_app(_config(tmp_path), repo_root=tmp_path)


def test_baseline_ratchet(tmp_path: Path) -> None:
    _project(tmp_path, baseline=True)
    config = _config(tmp_path)

    assert [f.rule for f in au.check_app(config, repo_root=tmp_path)] == [
        "api-route-uncalled"
    ]  # no baseline file yet
    assert au.check_app(config, repo_root=tmp_path, update_baseline=True) == []
    assert "GET /api/dead" in (tmp_path / "api-usage-baseline.txt").read_text()
    assert au.check_app(config, repo_root=tmp_path) == []  # tolerated now

    # a new dead route is reported; a baseline line whose route vanished is reported as stale
    _write(
        tmp_path,
        "openapi.json",
        json.dumps({"paths": {"/api/used": {"get": {}}, "/api/newdead": {"get": {}}}}),
    )
    findings = au.check_app(config, repo_root=tmp_path)
    assert sorted(
        (f.rule, "GET /api/newdead" in f.message, "GET /api/dead" in f.message)
        for f in findings
    ) == [
        ("api-route-baseline-stale", False, True),
        ("api-route-uncalled", True, False),
    ]
    assert (
        "no longer a backend route"
        in next(f for f in findings if f.rule == "api-route-baseline-stale").message
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [("called", "now used"), ("excluded", "now excluded")],
)
def test_baseline_stale_reason(tmp_path: Path, change: str, reason: str) -> None:
    _project(tmp_path, baseline=True)
    config = _config(tmp_path)
    au.check_app(config, repo_root=tmp_path, update_baseline=True)
    if change == "called":
        _write(tmp_path, "fe/src/b.ts", "fetch('/api/dead');\n")
    else:
        config = au.AppConfig(
            **{
                **{f: getattr(config, f) for f in config.__slots__},
                "exclude_tags": ["t"],
            }
        )
    findings = au.check_app(config, repo_root=tmp_path)
    assert [f.rule for f in findings] == ["api-route-baseline-stale"]
    assert reason in findings[0].message


def test_update_baseline_validates_all_apps_before_writing(tmp_path: Path) -> None:
    _project(tmp_path, baseline=True)
    table = load_bdt_table("dead_code", tmp_path)
    second = {
        "name": "second",
        "openapi": "openapi.json",
        "frontends": ["fe/src"],
    }  # no baseline
    with pytest.raises(au.ApiUsageError, match="missing for: second"):
        au.run(
            {"apps": [*table["apps"], second]}, repo_root=tmp_path, update_baseline=True
        )
    assert not (tmp_path / "api-usage-baseline.txt").exists()

    shared = {**second, "baseline": "api-usage-baseline.txt"}
    with pytest.raises(au.ApiUsageError, match="share the same"):
        au.run(
            {"apps": [*table["apps"], shared]}, repo_root=tmp_path, update_baseline=True
        )
    assert not (tmp_path / "api-usage-baseline.txt").exists()


def test_update_baseline_creates_parent_directories(tmp_path: Path) -> None:
    _project(tmp_path)
    config = au.AppConfig(
        name="m",
        openapi="openapi.json",
        frontends=["fe/src"],
        baseline="deep/dir/bl.txt",
    )
    au.check_app(config, repo_root=tmp_path, update_baseline=True)
    assert (tmp_path / "deep/dir/bl.txt").is_file()


# ------------------------------------------------------------------ routes referenced by name (url_for)


def _op_id(
    method: str, path: str, operation_id: str, *, mount: str = ""
) -> au.Operation:
    return au.Operation(method, path, (), mount, operation_id)


def test_read_referenced_route_names(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "backend/auth.py",
        """
        url = request.url_for("auth_callback")
        other = app.url_path_for(name='login')
        mounted = request.url_for("sub:inner")
        dynamic = request.url_for(provider)
        also = request.url_for(
            "multi_line",
        )
        nope = my_url_for("not_a_call")
        """,
    )
    _write(
        tmp_path,
        "backend/templates/page.html",
        "<a href=\"{{ url_for('from_template', id=1) }}\">x</a>",
    )
    _write(tmp_path, "backend/tests/test_auth.py", 'url_for("only_in_tests")\n')
    _write(tmp_path, "backend/test_x.py", 'url_for("only_in_test_file")\n')
    _write(tmp_path, "backend/node_modules/x.py", 'url_for("vendored")\n')
    assert au.read_referenced_route_names(tmp_path / "backend") == {
        "auth_callback",
        "login",
        "inner",
        "multi_line",
        "from_template",
    }
    assert au.read_referenced_route_names(tmp_path / "backend", ("my_url_for",)) == {
        "not_a_call"
    }


@pytest.mark.parametrize(
    ("op", "names", "expected"),
    [
        # FastAPI's default operationId: route name + path with non-word characters as `_`, + method
        (_op_id("GET", "/auth/login", "login_auth_login_get"), {"login"}, True),
        (_op_id("GET", "/auth/cb/{x}", "auth_cb_auth_cb__x__get"), {"auth_cb"}, True),
        (
            _op_id("GET", "/api/sub/items", "list_items_items_get", mount="/api/sub"),
            {"list_items"},
            True,
        ),
        (_op_id("GET", "/auth/login", "login_auth_login_get"), {"logout"}, False),
        (
            _op_id("GET", "/auth/login", "login_auth_login_get"),
            {"log"},
            False,
        ),  # a prefix of the name is not the name
        (
            _op_id("POST", "/auth/login", "login_auth_login_get"),
            {"login"},
            False,
        ),  # method is part of the id
        # custom generate_unique_id
        (_op_id("GET", "/auth/login", "login"), {"login"}, True),
        (_op_id("GET", "/auth/login", "auth-login"), {"login"}, True),
        (_op_id("GET", "/auth/login", "auth-relogin"), {"login"}, False),
        (
            _op("GET", "/auth/login"),
            {"login"},
            False,
        ),  # no operationId (older openapi file): nothing to go on
    ],
)
def test_is_named_by(op: au.Operation, names: set[str], expected: bool) -> None:
    assert au.is_named_by(op, names) is expected


def test_route_referenced_by_url_for_is_not_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "openapi.json",
        json.dumps(
            {
                "paths": {
                    "/auth/callback": {
                        "get": {"operationId": "auth_callback_auth_callback_get"}
                    },
                    "/auth/dead": {"get": {"operationId": "dead_auth_dead_get"}},
                }
            }
        ),
    )
    _write(tmp_path, "fe/src/a.ts", "export {};\n")
    _write(
        tmp_path, "backend/auth.py", 'redirect_uri = request.url_for("auth_callback")\n'
    )
    _write(
        tmp_path,
        "pyproject.toml",
        """
        [[tool.bdt.dead_code.apps]]
        openapi = "openapi.json"
        app_dir = "backend"
        frontends = ["fe/src"]
        """,
    )
    findings = au.check_app(_config(tmp_path), repo_root=tmp_path)
    assert [f.message.split(" ")[:2] for f in findings] == [["GET", "/auth/dead"]]


def test_url_for_functions_are_configurable(tmp_path: Path) -> None:
    _project(tmp_path, extra='url_for_functions = ["reverse"]')
    _write(
        tmp_path,
        "openapi.json",
        json.dumps({"paths": {"/api/x": {"get": {"operationId": "x_api_x_get"}}}}),
    )
    _write(tmp_path, "svc.py", 'reverse("x")\n')
    assert au.check_app(_config(tmp_path), repo_root=tmp_path) == []
    _write(
        tmp_path, "svc.py", 'url_for("x")\n'
    )  # not one of the configured functions any more
    assert len(au.check_app(_config(tmp_path), repo_root=tmp_path)) == 1


# ------------------------------------------------------------------ CLI


def test_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["dead-code"])
    assert result.exit_code == 1
    assert "GET /api/dead" in result.output
    assert "bdt lint:" not in result.output  # not the `bdt lint` summary line

    _write(tmp_path, "fe/src/b.ts", "fetch('/api/dead');\n")
    clean = runner.invoke(app, ["dead-code"])
    assert clean.exit_code == 0
    assert "no dead code found" in clean.output

    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    broken = runner.invoke(app, ["dead-code"])
    assert broken.exit_code == 2
    assert "nothing to check" in broken.output

    _write(tmp_path, "pyproject.toml", "[project\n")
    assert (
        runner.invoke(app, ["dead-code"]).exit_code == 2
    )  # invalid TOML is a setup error, not a traceback


def test_cli_runs_sql_and_routes_checks_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    _project(tmp_path, extra='\n[tool.bdt.dead_code]\nsql_roots = ["sql"]')
    _write(tmp_path, "sql/q/orphan.sql", "select 1")
    monkeypatch.chdir(tmp_path)
    both = runner.invoke(app, ["dead-code"])
    assert both.exit_code == 1
    assert "orphan.sql" in both.output
    assert "GET /api/dead" in both.output
    assert "2 issue(s) found" in both.output
    sql_only = runner.invoke(app, ["dead-code", "--only", "sql"])
    assert "orphan.sql" in sql_only.output
    assert "GET /api/dead" not in sql_only.output
    routes_only = runner.invoke(app, ["dead-code", "--only", "routes"])
    assert "GET /api/dead" in routes_only.output
    assert "orphan.sql" not in routes_only.output


def test_cli_update_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _project(tmp_path, baseline=True)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["dead-code"]).exit_code == 1
    updated = runner.invoke(app, ["dead-code", "--update-baseline"])
    assert updated.exit_code == 0
    assert "baseline(s) updated" in updated.output
    assert runner.invoke(app, ["dead-code"]).exit_code == 0
