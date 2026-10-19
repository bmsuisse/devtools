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
    monkeypatch.setattr("bmsdna.devtools.ado_auth.get_azure_token", lambda scope: scopes.append(scope) or "tokenvalue")

    assert get_az_devops_token() == "tokenvalue"
    assert scopes == [f"{ADO_RESOURCE_ID}/.default"]


def test_auth_header_without_pat_is_bearer(monkeypatch) -> None:
    monkeypatch.setattr("bmsdna.devtools.ado_auth.get_azure_token", lambda scope: "tok")

    assert auth_header(None) == {"Authorization": "Bearer tok"}


def test_get_azure_token_exits_with_hint_on_auth_failure(monkeypatch) -> None:
    from azure.core.exceptions import ClientAuthenticationError

    from bmsdna.devtools import azure_auth

    class FailingCredential:
        def get_token(self, *scopes, **kwargs):
            raise ClientAuthenticationError("no credential")

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", FailingCredential)

    with pytest.raises(SystemExit) as exc_info:
        azure_auth.get_azure_token("scope")

    assert "az login" in str(exc_info.value)
