import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

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
"""


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def _usage(root: Path, *frontends: str, exclude_globs: list[str] | None = None) -> au.FrontendUsage:
    dirs = [root / f for f in frontends]
    return au.scan_frontend(au.iter_frontend_files(dirs, repo_root=root, exclude_globs=exclude_globs or []))


def _op(method: str, path: str, *tags: str, mount: str = "") -> au.Operation:
    return au.Operation(method, path, tuple(tags), mount)


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
    text, end = au._read_template(src, 0)
    assert text == "/api/m/{}/att{}"
    assert src[end:] == " + rest"


# ------------------------------------------------------------------ backend inventory


def test_operations_from_openapi_prefixes_mount_and_keeps_tags() -> None:
    doc = {"paths": {"/a/{id}": {"get": {"tags": ["t"]}, "post": {}, "parameters": []}}}
    ops = au.operations_from_openapi(doc, mount="/api/sub")
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


def test_collect_app_operations_recurses_into_mounts_only() -> None:
    sub = _FakeApp({"/items": {"get": {}}})
    static = object()  # a StaticFiles-like mount: no openapi()
    root = _FakeApp({"/health": {"get": {}}}, [_FakeMount("/api/sub/", sub), _FakeMount("/assets", static), object()])
    assert sorted(o.key for o in au.collect_app_operations(root)) == ["GET /api/sub/items", "GET /health"]


def test_load_app_operations_runs_in_subprocess_with_cwd_on_path(tmp_path: Path) -> None:
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
    assert [o.key for o in au.load_app_operations("linkedapp:app", cwd=tmp_path)] == ["GET /api/linked"]


def test_load_app_operations_reports_import_errors(tmp_path: Path) -> None:
    _write(tmp_path, "boom.py", "raise RuntimeError('missing env var FOO')\n")
    with pytest.raises(au.ApiUsageError, match="missing env var FOO"):
        au.load_app_operations("boom:app", cwd=tmp_path)


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
    assert [o.path for o in au.load_app_operations("envapp:app", cwd=tmp_path, env={"ROUTE_NAME": "hello"})] == ["/hello"]


# ------------------------------------------------------------------ frontend evidence


def test_sdk_function_and_react_query_helper_count_as_calls(tmp_path: Path) -> None:
    _write(tmp_path, "fe/src/lib/generated/sdk.gen.ts", _SDK)
    _write(
        tmp_path,
        "fe/src/page.tsx",
        "import { listThingsOptions } from '@/lib/generated/@tanstack/react-query.gen';\nuseQuery(listThingsOptions());\n",
    )
    _write(tmp_path, "fe/src/other.ts", "import { getThing } from './lib/generated';\ngetThing({ path: { thing_id: 1 } });\n")
    usage = _usage(tmp_path, "fe/src")
    sdk = au.read_sdk_functions([tmp_path / "fe/src"])
    assert set(sdk) == {"getThing", "deleteThing", "listThings"}
    assert au.call_evidence(_op("GET", "/api/things"), usage, sdk) == "sdk"
    assert au.call_evidence(_op("GET", "/api/things/{thing_id}"), usage, sdk) == "sdk"
    # same path, different method: deleteThing is never referenced
    assert au.call_evidence(_op("DELETE", "/api/things/{thing_id}"), usage, sdk) is None


def test_generated_code_and_tests_are_not_callers(tmp_path: Path) -> None:
    _write(tmp_path, "fe/src/lib/generated/sdk.gen.ts", _SDK)
    _write(tmp_path, "fe/src/lib/generated/react-query.gen.ts", "export const x = () => deleteThing();\n")
    _write(tmp_path, "fe/src/lib/api-types.generated.ts", '"/api/things/{thing_id}": { delete: never };\n')
    _write(tmp_path, "fe/src/lib/types.d.ts", 'declare const u: "/api/things/{thing_id}";\n')
    _write(tmp_path, "fe/src/page.test.tsx", "deleteThing(); fetch('/api/things/1');\n")
    _write(tmp_path, "fe/src/__tests__/a.ts", "deleteThing();\n")
    _write(tmp_path, "fe/src/e2e/a.ts", "deleteThing();\n")
    usage = _usage(tmp_path, "fe/src")
    assert usage.files == 0
    sdk = au.read_sdk_functions([tmp_path / "fe/src"])
    assert au.call_evidence(_op("DELETE", "/api/things/{thing_id}"), usage, sdk) is None
    assert au.call_evidence(_op("GET", "/api/things/{thing_id}"), usage, sdk) is None


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
    usage = _usage(tmp_path, "fe/src")
    assert au.call_evidence(_op("GET", "/api/things/{thing_id}"), usage, {}) == "fetch"
    assert au.call_evidence(_op("POST", "/api/things"), usage, {}) == "fetch"
    # the literal exists, but only as a GET -> a DELETE of the same path still counts as 'url' evidence via
    # the literal (method-agnostic by design); asserting that documented limitation here
    assert au.call_evidence(_op("DELETE", "/api/things/{thing_id}"), usage, {}) == "url"


def test_handwritten_fetch_with_nested_template_and_apostrophes_in_comments(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "fe/src/mail.ts",
        """
        // don't desync the scanner with this apostrophe
        const label = "it's fine";
        const url = `/api/offer-parser/mail/${encodeURIComponent(mail.id)}/attachment${mailbox ? `?mailbox=${encodeURIComponent(mailbox)}` : ""}`;
        """,
    )
    usage = _usage(tmp_path, "fe/src")
    assert au.call_evidence(_op("GET", "/api/offer-parser/mail/{message_id}/attachment"), usage, {}) == "url"


def test_suffix_match_for_base_url_relative_clients(tmp_path: Path) -> None:
    _write(tmp_path, "fe/src/c.ts", "const r = await http.get(`/customers/${id}/sales`);\n")
    usage = _usage(tmp_path, "fe/src")
    assert au.call_evidence(_op("GET", "/api/customers/{customer_id}/sales"), usage, {}) == "url-sfx"
    assert au.call_evidence(_op("GET", "/api/customers/{customer_id}/other"), usage, {}) is None


def test_mount_prefix_is_stripped_for_sdk_urls(tmp_path: Path) -> None:
    _write(tmp_path, "fe/src/gen/sdk.gen.ts", _SDK.replace('"/api/things"', '"/things"'))
    _write(tmp_path, "fe/src/p.ts", "listThings();\n")
    usage = _usage(tmp_path, "fe/src")
    sdk = au.read_sdk_functions([tmp_path / "fe/src"])
    # a sub-app's generated client uses mount-relative urls ("/things"); the operation carries the mount ("/api/sub/things")
    assert au.call_evidence(_op("GET", "/api/sub/things", mount="/api/sub"), usage, sdk) == "sdk"
    assert au.call_evidence(_op("GET", "/things"), usage, sdk) == "sdk"
    assert au.call_evidence(_op("GET", "/api/sub/other", mount="/api/sub"), usage, sdk) is None


# ------------------------------------------------------------------ excludes


def test_find_uncalled_applies_prefix_tag_and_glob_excludes() -> None:
    ops = [
        _op("GET", "/external_api/a"),
        _op("GET", "/api/agent-tool", "agent"),
        _op("GET", "/auth/callback"),
        _op("POST", "/api/svc/sync"),
        _op("GET", "/api/dead"),
    ]
    dead = au.find_uncalled(
        ops,
        au.FrontendUsage(),
        {},
        exclude_prefixes=["/external_api"],
        exclude_tags=["agent"],
        exclude_paths=["/auth/*", "POST /api/svc/*"],
    )
    assert [o.key for o in dead] == ["GET /api/dead"]


# ------------------------------------------------------------------ config / orchestration


def test_parse_config_validation() -> None:
    with pytest.raises(au.ApiUsageError, match="no .*apps"):
        au.parse_config({})
    with pytest.raises(au.ApiUsageError, match="exactly one"):
        au.parse_config({"apps": [{"name": "x", "frontends": ["fe"]}]})
    with pytest.raises(au.ApiUsageError, match="exactly one"):
        au.parse_config({"apps": [{"name": "x", "app": "a:b", "openapi": "o.json", "frontends": ["fe"]}]})
    with pytest.raises(au.ApiUsageError, match="frontends"):
        au.parse_config({"apps": [{"name": "x", "app": "a:b"}]})


def _project(root: Path, *, baseline: bool = False) -> None:
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
    extra = 'baseline = "api-usage-baseline.txt"\n' if baseline else ""
    _write(
        root,
        "pyproject.toml",
        f"""
        [project]
        name = "x"

        [[tool.bdt.api_usage.apps]]
        name = "main"
        openapi = "openapi.json"
        frontends = ["fe/src"]
        exclude_prefixes = ["/external"]
        {extra}
        """,
    )


def test_check_app_reports_uncalled_routes(tmp_path: Path) -> None:
    _project(tmp_path)
    config = au.parse_config(load_bdt_table("api_usage", tmp_path))[0]
    findings = au.check_app(config, repo_root=tmp_path)
    assert [(f.rule, f.message.split(" is never")[0]) for f in findings] == [("api-route-uncalled", "GET /api/dead [t]")]


def test_frontends_glob_must_match(tmp_path: Path) -> None:
    _project(tmp_path)
    config = au.AppConfig(name="m", openapi="openapi.json", frontends=["nope/*/src"])
    with pytest.raises(au.ApiUsageError, match="matches no directory"):
        au.check_app(config, repo_root=tmp_path)


def test_baseline_ratchet(tmp_path: Path) -> None:
    _project(tmp_path, baseline=True)
    config = au.parse_config(load_bdt_table("api_usage", tmp_path))[0]

    assert [f.rule for f in au.check_app(config, repo_root=tmp_path)] == ["api-route-uncalled"]  # no baseline file yet
    assert au.check_app(config, repo_root=tmp_path, update_baseline=True) == []
    baseline = tmp_path / "api-usage-baseline.txt"
    assert "GET /api/dead" in baseline.read_text()
    assert au.check_app(config, repo_root=tmp_path) == []  # tolerated now

    # a new dead route is reported; a baseline line whose route vanished is reported as stale
    _write(tmp_path, "openapi.json", json.dumps({"paths": {"/api/used": {"get": {}}, "/api/newdead": {"get": {}}}}))
    findings = au.check_app(config, repo_root=tmp_path)
    assert sorted((f.rule, "GET /api/newdead" in f.message, "GET /api/dead" in f.message) for f in findings) == [
        ("api-route-baseline-stale", False, True),
        ("api-route-uncalled", True, False),
    ]
    assert "no longer a backend route" in next(f for f in findings if f.rule == "api-route-baseline-stale").message

    # a baseline line whose route is now called is stale too
    _write(tmp_path, "openapi.json", json.dumps({"paths": {"/api/used": {"get": {}}, "/api/dead": {"get": {}}}}))
    _write(tmp_path, "fe/src/b.ts", "fetch('/api/dead');\n")
    findings = au.check_app(config, repo_root=tmp_path)
    assert [f.rule for f in findings] == ["api-route-baseline-stale"]
    assert "now called" in findings[0].message


def test_update_baseline_requires_configured_baseline(tmp_path: Path) -> None:
    _project(tmp_path)
    config = au.parse_config(load_bdt_table("api_usage", tmp_path))[0]
    with pytest.raises(au.ApiUsageError, match="no `baseline`"):
        au.check_app(config, repo_root=tmp_path, update_baseline=True)


# ------------------------------------------------------------------ CLI


def test_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lint-api-usage"])
    assert result.exit_code == 1
    assert "GET /api/dead" in result.output

    _write(tmp_path, "fe/src/b.ts", "fetch('/api/dead');\n")
    assert runner.invoke(app, ["lint-api-usage"]).exit_code == 0

    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    broken = runner.invoke(app, ["lint-api-usage"])
    assert broken.exit_code == 2
    assert "no [[tool.bdt.api_usage.apps]]" in broken.output
