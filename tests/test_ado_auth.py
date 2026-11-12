import base64

import pytest

from bmsdna.devtools.ado_auth import ADO_RESOURCE_ID, auth_header, get_az_devops_token


def test_auth_header_uses_pat_without_azure_credential(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("must not ask azure-identity for a token when a PAT is given")

    monkeypatch.setattr("bmsdna.devtools.ado_auth.get_azure_token", fail)

    token = base64.b64encode(b":my-pat").decode()
    assert auth_header("my-pat") == {"Authorization": f"Basic {token}"}


def test_get_az_devops_token_asks_for_the_ado_scope(monkeypatch) -> None:
    scopes: list[str] = []
    monkeypatch.setattr("bmsdna.devtools.ado_auth.get_azure_token", lambda scope, hint=None: scopes.append(scope) or "tokenvalue")

    assert get_az_devops_token() == "tokenvalue"
    assert scopes == [f"{ADO_RESOURCE_ID}/.default"]


def test_auth_header_without_pat_is_bearer(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.ado_auth.get_azure_token", lambda scope, hint=None: "tok")

    assert auth_header(None) == {"Authorization": "Bearer tok"}


def test_get_azure_token_exits_with_hint_on_auth_failure(monkeypatch) -> None:
    from azure.core.exceptions import ClientAuthenticationError

    from bmsdna.devtools import azure_auth

    class FailingCredential:
        def get_token(self, *scopes, **kwargs):
            raise ClientAuthenticationError("no credential")

    monkeypatch.setattr(azure_auth, "_credential", lambda: FailingCredential())

    with pytest.raises(SystemExit) as exc_info:
        azure_auth.get_azure_token("scope")

    assert "az login" in str(exc_info.value)


def test_credential_is_built_once_with_the_cli_process_timeout(monkeypatch) -> None:
    from bmsdna.devtools import azure_auth
    from bmsdna.devtools.cli_tools import CLI_TIMEOUT_SECS

    built: list[dict] = []

    class Credential:
        def __init__(self, **kwargs):
            built.append(kwargs)

        def get_token(self, *scopes, **kwargs):
            assert kwargs == {}  # AzureCliCredential ignores a per-call timeout; it is a constructor option
            return type("Token", (), {"token": "t"})()

    azure_auth._credential.cache_clear()
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", Credential)
    try:
        assert azure_auth.get_azure_token("a") == "t"
        assert azure_auth.get_azure_token("b") == "t"
    finally:
        azure_auth._credential.cache_clear()

    assert built == [{"process_timeout": CLI_TIMEOUT_SECS}]


def _response(status: int, url: str, body: bytes = b"", content_type: str = "application/json", method: str = "POST"):
    import requests

    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = body
    response.headers["Content-Type"] = content_type
    response.request = requests.Request(method, url).prepare()
    return response


BUILD_URL = "https://dev.azure.com/bmeurope/My%20Project/_apis/build/builds/5"


def test_rejected_pat_error_names_scope_pat_sources_and_where_to_fix_it() -> None:
    from bmsdna.devtools.ado_auth import AdoAuthError, _check_auth_hook

    response = _check_auth_hook(True)(_response(401, BUILD_URL, b'{"message": "TF400813: not authorized"}'))

    with pytest.raises(AdoAuthError) as exc_info:
        response.raise_for_status()
    message = str(exc_info.value)
    assert "HTTP 401" in message and "POST" in message
    assert "TF400813: not authorized" in message
    assert "AZURE_DEVOPS_EXT_PAT" in message and "--pat" in message
    assert "Build (Read & execute)" in message
    assert "expired" in message and "different organization than 'bmeurope'" in message
    assert "https://dev.azure.com/bmeurope/_usersSettings/tokens" in message
    assert exc_info.value.response is response


def test_403_blames_the_scope_or_the_account_not_an_expired_pat() -> None:
    from bmsdna.devtools.ado_auth import AdoAuthError, _check_auth_hook

    response = _check_auth_hook(True)(_response(403, "https://dev.azure.com/bmeurope/p/_apis/git/repositories/r/pullRequests/1/threads"))

    with pytest.raises(AdoAuthError) as exc_info:
        response.raise_for_status()
    message = str(exc_info.value)
    assert "HTTP 403" in message and "Code (Read & write)" in message
    assert "your account itself lacks the permission" in message
    assert "expired" not in message


def test_login_token_error_says_to_az_login_or_use_a_pat() -> None:
    from bmsdna.devtools.ado_auth import AdoAuthError, _check_auth_hook

    response = _check_auth_hook(False)(_response(401, BUILD_URL))

    with pytest.raises(AdoAuthError) as exc_info:
        response.raise_for_status()
    message = str(exc_info.value)
    assert "`az login` token (no PAT given)" in message
    assert "az login" in message and "AZURE_DEVOPS_EXT_PAT" in message


@pytest.mark.parametrize(
    "status, content_type",
    [(203, "text/html"), (200, "text/html; charset=utf-8")],
)
def test_sign_in_page_counts_as_a_rejected_credential(status: int, content_type: str) -> None:
    from bmsdna.devtools.ado_auth import AdoAuthError, _check_auth_hook

    response = _check_auth_hook(True)(_response(status, BUILD_URL, b"<html>Sign in</html>", content_type))

    assert response.status_code == 401
    with pytest.raises(AdoAuthError):
        response.raise_for_status()


def test_other_responses_are_left_alone() -> None:
    from bmsdna.devtools.ado_auth import _check_auth_hook

    ok = _check_auth_hook(True)(_response(200, BUILD_URL, b"{}"))
    missing = _check_auth_hook(True)(_response(404, BUILD_URL))
    page = _check_auth_hook(True)(_response(200, "https://dev.azure.com/o/p/_git/r", b"<html>", "text/html"))

    ok.raise_for_status()
    assert missing.status_code == 404 and page.status_code == 200
    with pytest.raises(Exception) as exc_info:
        missing.raise_for_status()
    assert type(exc_info.value).__name__ == "HTTPError"


def test_ado_session_authenticates_and_converts_rejections(monkeypatch) -> None:
    from bmsdna.devtools.ado_auth import AdoAuthError, ado_session

    session = ado_session("my-pat")

    assert session.headers["Authorization"].startswith("Basic ")
    response = _response(401, BUILD_URL)
    for hook in session.hooks["response"]:
        hook(response)
    with pytest.raises(AdoAuthError):
        response.raise_for_status()


def test_missing_credentials_message_explains_both_ways_out(monkeypatch) -> None:
    from azure.core.exceptions import ClientAuthenticationError

    from bmsdna.devtools import azure_auth
    from bmsdna.devtools.gitrepo import AdoRemote

    class FailingCredential:
        def get_token(self, *scopes, **kwargs):
            raise ClientAuthenticationError("DefaultAzureCredential failed\nEnvironmentCredential: no env vars\nAzureCliCredential: not logged in")

    monkeypatch.setattr(azure_auth, "_credential", lambda: FailingCredential())
    monkeypatch.setattr("bmsdna.devtools.ado_auth.current_remote", lambda: AdoRemote("bmeurope", "p", "r"))

    with pytest.raises(SystemExit) as exc_info:
        get_az_devops_token()

    message = str(exc_info.value)
    assert "no PAT was given" in message
    assert "https://dev.azure.com/bmeurope/_usersSettings/tokens" in message
    assert "Build (Read & execute)" in message and "`az login`" in message
    assert "AzureCliCredential" not in message  # only the first line of azure-identity's per-credential dump
