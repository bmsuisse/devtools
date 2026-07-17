"""Azure DevOps auth: explicit PAT, or fall back to the caller's `az` login."""

from __future__ import annotations

import base64
import subprocess
import sys

from .cli_tools import require_az

# Well-known Azure DevOps resource ID for `az account get-access-token`.
ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"


def get_az_devops_token() -> str:
    az = require_az()
    result = subprocess.run(
        [az, "account", "get-access-token", "--resource", ADO_RESOURCE_ID, "--query", "accessToken", "-o", "tsv"],
        capture_output=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        sys.exit(f"az login required and no PAT provided.\n{result.stderr.strip()}")
    return result.stdout.strip()


def auth_header(pat: str | None) -> dict[str, str]:
    """Basic-auth header for an explicit PAT, else Bearer via `az` token.

    Never hardcode a PAT literal in a caller — pass it in from an env var
    (e.g. AZURE_DEVOPS_PAT) or a CLI flag, or omit it and let `az` supply a
    short-lived token from the operator's own login.
    """
    if pat:
        token = base64.b64encode(f":{pat}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {"Authorization": f"Bearer {get_az_devops_token()}"}
