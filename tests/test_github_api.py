import pytest
import requests

from bmsdna.devtools import github_api
from bmsdna.devtools.github_api import GitHubClient


class FakeResponse:
    def __init__(self, status=200, body=None, links=None, text=""):
        self.status_code = status
        self._body = body
        self.links = links or {}
        self.reason = "reason"
        self.text = text
        self.content = b"x" if body is not None else b""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def client_with(monkeypatch, responses):
    client = GitHubClient("tok", "o", "r")
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(client.session, "request", fake_request)
    return client, calls


def test_token_prefers_env_over_cli(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "from-env")
    monkeypatch.setattr(github_api, "_token_from_gh_cli", lambda: pytest.fail("must not call gh"))
    assert github_api.resolve_token() == "from-env"


def test_token_falls_back_to_gh_cli(monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github_api, "_token_from_gh_cli", lambda: "from-cli")
    assert github_api.resolve_token() == "from-cli"


def test_token_missing_exits_with_hint(monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github_api, "_token_from_gh_cli", lambda: None)
    with pytest.raises(SystemExit, match="GH_TOKEN"):
        github_api.resolve_token()


def test_error_status_exits_with_api_message(monkeypatch) -> None:
    client, _ = client_with(monkeypatch, [FakeResponse(422, {"message": "Validation Failed", "errors": [{"message": "bad label"}]})])
    with pytest.raises(SystemExit, match="Validation Failed.*bad label"):
        client.get("repos/o/r/issues")


def test_ok_status_is_returned_not_raised(monkeypatch) -> None:
    client, _ = client_with(monkeypatch, [FakeResponse(404, {"message": "Not Found"})])
    assert client.request("GET", "x", ok=(404,)).status_code == 404


def test_network_error_exits(monkeypatch) -> None:
    client = GitHubClient("tok", "o", "r")

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(client.session, "request", boom)
    with pytest.raises(SystemExit, match="down"):
        client.get("x")


def test_paginate_follows_next_links_and_honors_limit(monkeypatch) -> None:
    pages = [
        FakeResponse(200, [1, 2], links={"next": {"url": "https://api.github.com/page2"}}),
        FakeResponse(200, [3, 4]),
    ]
    client, calls = client_with(monkeypatch, pages)
    assert client.paginate("repos/o/r/things") == [1, 2, 3, 4]
    assert calls[0][2]["params"] == {"per_page": 100}
    assert calls[1][1] == "https://api.github.com/page2" and calls[1][2]["params"] is None

    client, _ = client_with(monkeypatch, [FakeResponse(200, {"items": [1, 2, 3]})])
    assert client.paginate("search/issues", items_key="items", limit=2) == [1, 2]


def test_graphql_raises_on_errors(monkeypatch) -> None:
    client, calls = client_with(monkeypatch, [FakeResponse(200, {"errors": [{"message": "nope"}]})])
    with pytest.raises(SystemExit, match="nope"):
        client.graphql("query { x }", a=1)
    assert calls[0][2]["json"] == {"query": "query { x }", "variables": {"a": 1}}


def test_repo_path_defaults_to_origin_remote(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.gitrepo.origin_url", lambda cwd=None: "git@github.com:acme/widgets.git")
    assert GitHubClient("tok").repo_path == "repos/acme/widgets"


def test_absolute_url_on_another_host_never_gets_the_token(monkeypatch) -> None:
    client, calls = client_with(monkeypatch, [FakeResponse(200, {})])
    with pytest.raises(SystemExit, match="Refusing"):
        client.request("GET", "https://evil.example.com/next")
    with pytest.raises(SystemExit, match="Refusing"):
        client.request("GET", "http://api.github.com/insecure")
    assert calls == []


def test_absolute_url_on_the_api_host_is_sent(monkeypatch) -> None:
    client, calls = client_with(monkeypatch, [FakeResponse(200, {"ok": 1})])
    assert client.get("https://api.github.com/repos/o/r/issues?page=2") == {"ok": 1}
    assert calls[0][1] == "https://api.github.com/repos/o/r/issues?page=2"


def test_get_retries_a_dropped_connection_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(github_api, "RETRY_BACKOFF_SECS", 0)
    client = GitHubClient("tok", "o", "r")
    responses = [requests.ConnectionError("reset"), requests.ConnectionError("reset"), FakeResponse(200, {"ok": 1})]
    calls: list[str] = []

    def flaky(method, url, **kwargs):
        calls.append(method)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(client.session, "request", flaky)
    assert client.get("x") == {"ok": 1}
    assert len(calls) == 3


def test_get_gives_up_after_the_attempt_limit(monkeypatch) -> None:
    monkeypatch.setattr(github_api, "RETRY_BACKOFF_SECS", 0)
    client = GitHubClient("tok", "o", "r")
    attempts: list[int] = []

    def always_down(*a, **k):
        attempts.append(1)
        raise requests.ConnectionError("down")

    monkeypatch.setattr(client.session, "request", always_down)
    with pytest.raises(SystemExit, match="down"):
        client.get("x")
    assert len(attempts) == github_api.RETRY_ATTEMPTS


def test_post_is_not_retried_but_a_graphql_query_is(monkeypatch) -> None:
    monkeypatch.setattr(github_api, "RETRY_BACKOFF_SECS", 0)
    client = GitHubClient("tok", "o", "r")
    attempts: list[str] = []

    def drop_then_ok(method, url, **kwargs):
        attempts.append(method)
        if len(attempts) % 2:
            raise requests.ConnectionError("reset")
        return FakeResponse(200, {"data": {"x": 1}})

    monkeypatch.setattr(client.session, "request", drop_then_ok)
    with pytest.raises(SystemExit):
        client.post("repos/o/r/pulls", {"title": "t"})  # a create must never be repeated blindly
    assert len(attempts) == 1
    attempts.clear()
    assert client.graphql("query { x }") == {"x": 1}
    assert len(attempts) == 2
    attempts.clear()
    with pytest.raises(SystemExit):
        client.graphql("mutation { y }")
    assert len(attempts) == 1


def test_503_is_retried(monkeypatch) -> None:
    monkeypatch.setattr(github_api, "RETRY_BACKOFF_SECS", 0)
    client, calls = client_with(monkeypatch, [FakeResponse(503, {"message": "unavailable"}), FakeResponse(200, {"ok": 1})])
    assert client.get("x") == {"ok": 1}
    assert len(calls) == 2
