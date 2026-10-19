import pytest

from bmsdna.devtools import azure_arm, logs

COMPONENTS = "/subscriptions/sub-1/resourceGroups/my-rg/providers/Microsoft.Insights/components"


class FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _table(columns, rows):
    return {"tables": [{"name": "PrimaryResult", "columns": [{"name": c} for c in columns], "rows": rows}]}


@pytest.fixture
def azure(monkeypatch):
    """Fake the ARM + query boundary; `state['queries']` records (url, scope, kql)."""
    state = {"queries": [], "arm": [], "result": _table([], []), "components": [{"name": "ai-1"}]}

    def fake_arm(method, path, api_version, **kw):
        state["arm"].append((method, path, api_version))
        if path == COMPONENTS:
            return {"value": state["components"]}
        if path.startswith(COMPONENTS + "/"):
            return {"properties": {"AppId": "app-guid"}}
        raise AssertionError(path)

    def fake_request(method, url, *, scope, json, **kw):
        state["queries"].append((method, url, scope, json["query"]))
        return FakeResp(state["result"])

    monkeypatch.setattr(azure_arm, "default_subscription_id", lambda: "sub-1")
    monkeypatch.setattr(azure_arm, "arm", fake_arm)
    monkeypatch.setattr(azure_arm, "request", fake_request)
    return state


def test_find_app_insights_returns_first_component_name(azure):
    azure["components"] = [{"name": "first"}, {"name": "second"}]
    assert logs.find_app_insights("my-rg") == "first"
    assert azure["arm"] == [("GET", COMPONENTS, "2020-02-02")]


def test_find_app_insights_none_when_empty_or_resource_group_not_found(azure, monkeypatch):
    azure["components"] = []
    assert logs.find_app_insights("my-rg") is None

    def not_found(*a, **k):
        raise azure_arm.AzureApiError("GET x failed (HTTP 404): ResourceGroupNotFound")

    monkeypatch.setattr(azure_arm, "arm", not_found)
    assert logs.find_app_insights("my-rg") is None


def test_find_app_insights_propagates_other_errors(azure, monkeypatch):
    def forbidden(*a, **k):
        raise azure_arm.AzureApiError("GET x failed (HTTP 403): AuthorizationFailed")

    monkeypatch.setattr(azure_arm, "arm", forbidden)
    with pytest.raises(azure_arm.AzureApiError, match="403"):
        logs.find_app_insights("my-rg")


def test_find_app_insights_propagates_subscription_errors(azure, monkeypatch):
    def ambiguous():
        raise azure_arm.AzureApiError("Multiple Azure subscriptions available")

    monkeypatch.setattr(azure_arm, "default_subscription_id", ambiguous)
    with pytest.raises(azure_arm.AzureApiError, match="Multiple"):
        logs.find_app_insights("my-rg")


def test_query_roles_sums_and_sorts(azure):
    azure["result"] = _table(["cloud_RoleName", "count_"], [["a", 2], [None, 5]])

    roles, err = logs.query_roles("ai-1", "my-rg", 30)

    assert err is None
    assert roles == {"(unknown)": 5, "a": 2}
    method, url, scope, kql = azure["queries"][0]
    assert url == "https://api.applicationinsights.io/v1/apps/app-guid/query"
    assert scope == "https://api.applicationinsights.io/.default"
    assert "ago(30m)" in kql


def test_query_roles_reports_missing_column(azure):
    azure["result"] = _table(["other"], [])
    roles, err = logs.query_roles("ai-1", "my-rg", 30)
    assert roles is None and "cloud_RoleName" in err


def test_query_logs_parses_rows_and_filters(azure):
    azure["result"] = _table(
        ["timestamp", "severityLevel", "message", "itemType", "type", "outerMessage"],
        [
            ["2024-01-01T10:00:00Z", 3, "bad", "trace", None, None],
            ["2024-01-01T10:00:01Z", 4, "", "exception", "KeyError", "'x'"],
        ],
    )

    rows, err = logs.query_logs("ai-1", "my-rg", 5, 2, "web")

    assert err is None
    assert rows == [
        {"timestamp": "2024-01-01T10:00:00Z", "lvl": "ERROR   ", "msg": "bad", "exc": ""},
        {"timestamp": "2024-01-01T10:00:01Z", "lvl": "CRITICAL", "msg": "", "exc": " | KeyError: 'x'"},
    ]
    kql = azure["queries"][0][3]
    assert "severityLevel >= 2" in kql and 'cloud_RoleName == "web"' in kql


def test_query_logs_role_all_omits_filter(azure):
    azure["result"] = _table(["timestamp"], [])
    logs.query_logs("ai-1", "my-rg", 5, 0, "all")
    assert "cloud_RoleName" not in azure["queries"][0][3]


def test_query_error_is_returned_not_raised(azure, monkeypatch):
    def boom(*a, **k):
        raise azure_arm.AzureApiError("HTTP 404: component not found")

    monkeypatch.setattr(azure_arm, "arm", boom)
    rows, err = logs.query_logs("missing", "my-rg", 5, 0, "all")
    assert rows is None and "component not found" in err


def test_print_logs_falls_back_to_discovered_app_insights(azure, capsys, monkeypatch):
    azure["result"] = _table(["timestamp", "severityLevel", "message"], [["2024-01-01T10:00:00Z", 1, "hello"]])
    real_arm = azure_arm.arm

    def arm_missing_named(method, path, api_version, **kw):
        if path.endswith("/components/wrong"):
            raise azure_arm.AzureApiError("not found")
        return real_arm(method, path, api_version, **kw)

    monkeypatch.setattr(azure_arm, "arm", arm_missing_named)

    logs.print_logs("wrong", "my-rg", 5, "verbose", "all", no_color=True)

    out = capsys.readouterr().out
    assert "10:00:00 [INFO    ] hello" in out


def test_print_logs_exits_when_nothing_found(azure, monkeypatch):
    azure["components"] = []

    def boom(method, path, api_version, **kw):
        if path == COMPONENTS:
            return {"value": []}
        raise azure_arm.AzureApiError("not found")

    monkeypatch.setattr(azure_arm, "arm", boom)

    with pytest.raises(SystemExit) as exc_info:
        logs.print_logs("wrong", "my-rg", 5, "verbose", "all", no_color=True)

    assert "No App Insights" in str(exc_info.value)
