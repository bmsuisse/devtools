"""Minimal raw-HTTP helpers for Azure Resource Manager and the Azure data-plane query APIs,
replacing `az ...` subprocess calls. Tokens come from `azure_auth.get_azure_token`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests

from .azure_auth import get_azure_token
from .cli_tools import HTTP_TIMEOUT_SECS

ARM_URL = "https://management.azure.com"
ARM_SCOPE = f"{ARM_URL}/.default"
SUBSCRIPTION_API_VERSION = "2022-12-01"
SUBSCRIPTION_HINT = "Set AZURE_SUBSCRIPTION_ID (or `az account set`) to pick one."


class AzureApiError(Exception):
    """A failed Azure REST call; `str(e)` carries the service's own message."""


def _error_message(r: requests.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text.strip() or r.reason or "request failed"
    err = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)
    return str(err)


def request(
    method: str,
    url: str,
    *,
    scope: str = ARM_SCOPE,
    params: dict[str, Any] | None = None,
    json: Any = None,
    timeout: float = HTTP_TIMEOUT_SECS,
    **kwargs: Any,
) -> requests.Response:
    """`method url` with a bearer token for `scope`; raises `AzureApiError` on a network error or non-2xx status."""
    headers = {"Authorization": f"Bearer {get_azure_token(scope)}", **kwargs.pop("headers", {})}
    try:
        r = requests.request(method, url, params=params, json=json, headers=headers, timeout=timeout, **kwargs)
    except requests.RequestException as e:
        raise AzureApiError(f"{method} {url} failed: {e}") from e
    if r.status_code >= 400:
        raise AzureApiError(f"{method} {url} failed (HTTP {r.status_code}): {_error_message(r)}")
    return r


def arm(method: str, path: str, api_version: str, *, json: Any = None, params: dict[str, Any] | None = None) -> Any:
    """JSON result of an ARM call; `path` is relative to the ARM root, e.g. `/subscriptions/...`."""
    r = request(method, f"{ARM_URL}/{path.lstrip('/')}", params={"api-version": api_version, **(params or {})}, json=json)
    return r.json() if r.content else None


def _az_profile_default_subscription() -> str | None:
    """The `isDefault` subscription of `az`'s profile (`az account set`), if there is one to read."""
    path = Path(os.environ.get("AZURE_CONFIG_DIR") or Path.home() / ".azure") / "azureProfile.json"
    try:
        profile = json.loads(path.read_text(encoding="utf-8-sig"))
        return next((s["id"] for s in profile["subscriptions"] if s.get("isDefault")), None)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def default_subscription_id() -> str:
    """The subscription to use: `AZURE_SUBSCRIPTION_ID`, else the default one of `az`'s profile
    (`az account set`), else the account's only enabled subscription (several: exits with the list)."""
    env = os.environ.get("AZURE_SUBSCRIPTION_ID")
    if env:
        return env
    profile_default = _az_profile_default_subscription()
    if profile_default:
        return profile_default
    try:
        page = arm("GET", "/subscriptions", SUBSCRIPTION_API_VERSION)
        subs = page.get("value", [])
        while page.get("nextLink"):
            r = request("GET", page["nextLink"])
            page = r.json()
            subs.extend(page.get("value", []))
    except AzureApiError as e:
        raise AzureApiError(f"Couldn't list Azure subscriptions (try `az login`?): {e}") from e
    enabled = [s for s in subs if s.get("state", "Enabled") == "Enabled"]
    if not enabled:
        raise AzureApiError("No enabled Azure subscription found for this account (try `az login`?).")
    if len(enabled) > 1:
        names = ", ".join(f"{s.get('displayName', '?')} ({s['subscriptionId']})" for s in enabled)
        raise AzureApiError(f"Multiple Azure subscriptions available: {names}. {SUBSCRIPTION_HINT}")
    return enabled[0]["subscriptionId"]
