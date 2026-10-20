"""Azure DevOps auth: explicit PAT (`--pat` / AZURE_DEVOPS_EXT_PAT / AZURE_DEVOPS_PAT), or a token from
`DefaultAzureCredential` (see `azure_auth`)."""

from __future__ import annotations

import base64

from .azure_auth import get_azure_token

# Well-known Azure DevOps resource ID; `<id>/.default` is its AAD scope.
ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"


def get_az_devops_token() -> str:
    return get_azure_token(f"{ADO_RESOURCE_ID}/.default")


def auth_header(pat: str | None) -> dict[str, str]:
    """Basic-auth header for an explicit PAT, else Bearer via `DefaultAzureCredential`.

    Never hardcode a PAT literal in a caller — pass it in from an env var
    (e.g. AZURE_DEVOPS_EXT_PAT) or a CLI flag, or omit it and let azure-identity supply a
    short-lived token from the operator's own login.
    """
    if pat:
        token = base64.b64encode(f":{pat}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {"Authorization": f"Bearer {get_az_devops_token()}"}
