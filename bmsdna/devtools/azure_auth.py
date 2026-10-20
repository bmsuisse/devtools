"""Azure AD tokens via `azure-identity`'s `DefaultAzureCredential` (env vars, managed identity, `az login`, ...),
replacing `az account get-access-token`."""

from __future__ import annotations

import functools
import sys

from .cli_tools import CLI_TIMEOUT_SECS


@functools.cache
def _credential():
    """One credential chain per process (it caches tokens). `process_timeout` bounds the `az` subprocess
    `AzureCliCredential` runs -- a cold start can exceed azure-identity's 10s default."""
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(process_timeout=CLI_TIMEOUT_SECS)


def get_azure_token(scope: str) -> str:
    """A bearer token for `scope` (e.g. `https://management.azure.com/.default`)."""
    try:
        from azure.core.exceptions import ClientAuthenticationError
    except ImportError:  # pragma: no cover -- a declared dependency
        sys.exit("`azure-identity` is required for Azure authentication: pip install azure-identity")
    try:
        return _credential().get_token(scope).token
    except ClientAuthenticationError as e:
        sys.exit(f"Azure authentication failed (try `az login`, or set AZURE_DEVOPS_EXT_PAT for Azure DevOps):\n{e}")
