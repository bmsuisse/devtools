import pytest
import requests

from bmsdna.devtools import azure_arm


class FakeResp:
    def __init__(self, status=200, body=None, reason="reason"):
        self.status_code = status
        self._body = body
        self.reason = reason
        self.text = ""
        self.content = b"x" if body is not None else b""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(azure_arm, "get_azure_token", lambda scope: f"tok:{scope}")
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)


def test_request_sends_bearer_token_for_scope(monkeypatch):
    seen = {}

    def fake(method, url, **kw):
        seen.update(method=method, url=url, **kw)
        return FakeResp(body={})

    monkeypatch.setattr(requests, "request", fake)

    azure_arm.request("GET", "https://x/y", scope="https://s/.default")

    assert seen["headers"]["Authorization"] == "Bearer tok:https://s/.default"


def test_request_raises_with_service_message(monkeypatch):
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResp(403, {"error": {"message": "denied"}}))

    with pytest.raises(azure_arm.AzureApiError, match="HTTP 403.*denied"):
        azure_arm.request("GET", "https://x/y")


def test_arm_pins_api_version(monkeypatch):
    seen = {}

    def fake(method, url, **kw):
        seen.update(url=url, **kw)
        return FakeResp(body={"ok": 1})

    monkeypatch.setattr(requests, "request", fake)

    assert azure_arm.arm("GET", "/subscriptions", "2022-12-01") == {"ok": 1}
    assert seen["url"] == "https://management.azure.com/subscriptions"
    assert seen["params"] == {"api-version": "2022-12-01"}


def _subs(monkeypatch, subs):
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResp(body={"value": subs}))


def test_default_subscription_prefers_env(monkeypatch):
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "env-sub")
    assert azure_arm.default_subscription_id() == "env-sub"


def test_default_subscription_single_enabled(monkeypatch):
    _subs(
        monkeypatch,
        [{"subscriptionId": "a", "state": "Disabled"}, {"subscriptionId": "b", "state": "Enabled"}],
    )
    assert azure_arm.default_subscription_id() == "b"


def test_default_subscription_ambiguous_errors(monkeypatch):
    _subs(
        monkeypatch,
        [
            {"subscriptionId": "a", "displayName": "A", "state": "Enabled"},
            {"subscriptionId": "b", "displayName": "B", "state": "Enabled"},
        ],
    )
    with pytest.raises(azure_arm.AzureApiError, match="AZURE_SUBSCRIPTION_ID"):
        azure_arm.default_subscription_id()


def test_default_subscription_none_errors(monkeypatch):
    _subs(monkeypatch, [])
    with pytest.raises(azure_arm.AzureApiError, match="No enabled"):
        azure_arm.default_subscription_id()
