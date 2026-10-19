"""A recording stand-in for `github_api.GitHubClient`: tests register canned answers per
`"METHOD path"` (a value, or a callable taking the request payload) and assert on `.calls`."""

from __future__ import annotations

from typing import Any, Callable

NOT_FOUND = object()


class FakeResponse:
    def __init__(self, status_code: int, data: Any) -> None:
        self.status_code = status_code
        self._data = data
        self.content = b"" if data is None else b"x"

    def json(self) -> Any:
        return self._data


class FakeGitHub:
    owner = "owner"
    repo = "repo"
    repo_path = "repos/owner/repo"

    def __init__(self, routes: dict[str, Any] | None = None, graphql: Callable[[str, dict], dict] | None = None) -> None:
        self.routes = routes or {}
        self.graphql_handler = graphql
        self.calls: list[tuple[str, str, Any]] = []

    def _answer(self, method: str, path: str, payload: Any) -> Any:
        self.calls.append((method, path, payload))
        value = self.routes.get(f"{method} {path}")
        return value(payload) if callable(value) else value

    def request(self, method: str, path: str, *, ok: tuple[int, ...] = (), **kwargs: Any) -> FakeResponse:
        value = self._answer(method, path, kwargs.get("json", kwargs.get("params")))
        if value is NOT_FOUND:
            if 404 not in ok:
                raise SystemExit(f"GitHub {method} {path} failed (HTTP 404)")
            return FakeResponse(404, None)
        return FakeResponse(200, value)

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path).json()

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, json=body).json()

    def patch(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self.request("PATCH", path, json=body).json()

    def put(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self.request("PUT", path, json=body).json()

    def delete(self, path: str, **kwargs: Any) -> None:
        self.request("DELETE", path)

    def paginate(self, path: str, params: dict[str, Any] | None = None, *, items_key: str | None = None, limit: int | None = None) -> list[Any]:
        value = self._answer("GET", path, {"params": params, "limit": limit})
        items = list(value or [])
        return items[:limit] if limit is not None else items

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        self.calls.append(("GRAPHQL", query, variables))
        assert self.graphql_handler, f"unexpected GraphQL call: {query}"
        return self.graphql_handler(query, variables)

    def calls_to(self, method: str, path: str | None = None) -> list[Any]:
        """Payloads of the recorded `method` calls (to `path`, if given)."""
        return [p for m, pa, p in self.calls if m == method and (path is None or pa == path)]
