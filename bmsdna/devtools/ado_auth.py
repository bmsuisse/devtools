"""Azure DevOps auth: explicit PAT (`--pat` / AZURE_DEVOPS_EXT_PAT / AZURE_DEVOPS_PAT), or a token from
`DefaultAzureCredential` (see `azure_auth`)."""

from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs, urlparse

import requests

from .azure_auth import get_azure_token
from .pr_cancel import one_line
from .gitrepo import AdoRemote, current_remote

# Well-known Azure DevOps resource ID; `<id>/.default` is its AAD scope.
ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"

PAT_SOURCES = "--pat / AZURE_DEVOPS_EXT_PAT / AZURE_DEVOPS_PAT"
# What a PAT needs for everything `bdt` does against Azure DevOps (PRs, builds, work items).
BDT_PAT_SCOPES = "Code (Read & write), Build (Read & execute), Work Items (Read & write)"

# REST API area -> the PAT scope a call into it needs at least
_SCOPE_BY_API = (
    ("/_apis/build/", "Build (Read & execute)"),
    ("/_apis/git/", "Code (Read & write)"),
    ("/_apis/policy/", "Code (Read & write)"),
    ("/_apis/wit/", "Work Items (Read & write)"),
    ("/_apis/userentitlements/", "MemberEntitlement Management (Read)"),
    ("/_apis/identities", "Identity (Read)"),
    ("/_apis/graph/", "Graph (Read)"),
)
_ORG_RE = re.compile(r"^/([^/]+)")


class AdoAuthError(requests.HTTPError):
    """Azure DevOps rejected the credentials (401), or accepted them but refuses the call (403): the message
    already says which PAT scope / login to fix, so it is meant to be shown as is."""


def pat_settings_url(org: str | None) -> str:
    if org:
        return f"https://dev.azure.com/{org}/_usersSettings/tokens"
    return "https://dev.azure.com/ (user settings > Personal access tokens)"


def _remote_org() -> str | None:
    """The org of the current checkout's `origin`, if it is an Azure DevOps one (best effort: auth also fails
    outside a checkout)."""
    try:
        remote = current_remote()
    except Exception:  # noqa: BLE001 -- not a git checkout / unknown remote: just no org to name
        return None
    return remote.org if isinstance(remote, AdoRemote) else None


def get_az_devops_token() -> str:
    org = _remote_org()
    hint = (
        "No Azure DevOps credentials: no PAT was given (--pat, AZURE_DEVOPS_EXT_PAT and AZURE_DEVOPS_PAT are all unset) "
        "and there is no usable `az login`.\n"
        "Fix it with either:\n"
        f"  - a PAT: create one at {pat_settings_url(org)} (scopes: {BDT_PAT_SCOPES}), then "
        "`export AZURE_DEVOPS_EXT_PAT=<token>` or pass --pat\n"
        "  - `az login` as a user with access to the organization"
    )
    return get_azure_token(f"{ADO_RESOURCE_ID}/.default", hint=hint)


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


def _ado_message(response: requests.Response) -> str | None:
    try:
        message = response.json().get("message")
    except (ValueError, AttributeError):
        return None
    return one_line(message) if isinstance(message, str) and message.strip() else None


def _is_sign_in_url(url: str) -> bool:
    return urlparse(url).path.rstrip("/").endswith("/_signin")


def _requested_url(response: requests.Response) -> str:
    """The URL that was asked for. A rejected PAT is redirected to a sign-in page, which is what `response.url` is then
    (and `response.history` is only filled in once the whole redirect chain is done, after the hook ran); the page
    carries the original URL as `reply_to`."""
    if response.history:
        return response.history[0].url
    if _is_sign_in_url(response.url):
        reply_to = parse_qs(urlparse(response.url).query).get("reply_to")
        if reply_to:
            return reply_to[0]
    return response.url


def auth_failure_message(response: requests.Response, pat_used: bool) -> str:
    """Why `response` (a 401/403 from Azure DevOps) happened and how to fix it, for the credentials in use."""
    parsed = urlparse(_requested_url(response))
    org_match = _ORG_RE.match(parsed.path) if parsed.netloc == "dev.azure.com" else None
    org = org_match.group(1) if org_match else _remote_org()
    scope = next((s for api, s in _SCOPE_BY_API if api in parsed.path), None)
    where = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    lines = []
    if response.status_code == 403:
        lines.append(f"Azure DevOps refused the call (HTTP 403) -- {where}")
    else:
        lines.append(f"Azure DevOps did not accept the credentials (HTTP 401) -- {where}")
    says = _ado_message(response)
    if says:
        lines.append(f"Azure DevOps says: {says}")
    if pat_used:
        lines.append(f"Credentials: the PAT from {PAT_SOURCES}.")
        causes: list[str] = ["it lacks the scope this call needs" + (f" ({scope})" if scope else "")]
        if response.status_code != 403:
            causes += ["it has expired or was revoked", "it was created for a different organization" + (f" than '{org}'" if org else "")]
        else:
            causes += ["your account itself lacks the permission"]
        lines.append("Most likely " + ", or ".join(causes) + ".")
        lines.append(f"Fix or recreate it at {pat_settings_url(org)} (scopes bdt needs: {BDT_PAT_SCOPES}).")
    else:
        lines.append("Credentials: your `az login` token (no PAT given).")
        lines.append(
            "Run `az login` as a user with access to " + (f"organization '{org}'" if org else "the organization")
            + f", or use a PAT instead: create one at {pat_settings_url(org)} (scopes: {BDT_PAT_SCOPES}) and set AZURE_DEVOPS_EXT_PAT / pass --pat."
        )
    return "\n".join(lines)


def _is_sign_in_page(response: requests.Response) -> bool:
    """Azure DevOps answers credentials it does not recognise by redirecting to a sign-in page (so 203, or 200 with
    HTML) -- for its REST API, which otherwise only ever speaks JSON/text."""
    if "/_apis/" not in _requested_url(response):
        return False
    if _is_sign_in_url(response.url):
        return True
    if response.status_code == 203:
        return True
    return response.status_code == 200 and "text/html" in response.headers.get("Content-Type", "")


def _check_auth_hook(pat_used: bool):
    def hook(response: requests.Response, *args, **kwargs) -> requests.Response:
        if _is_sign_in_page(response):
            # one status for "not signed in", however Azure DevOps phrased it, so callers' `>= 400` checks agree
            response.status_code = 401
        if response.status_code in (401, 403):
            error = AdoAuthError(auth_failure_message(response, pat_used), response=response)

            def raise_for_status() -> None:
                raise error

            # replaces the generic "401 Client Error: Unauthorized for url" `raise_for_status` would raise
            setattr(response, "raise_for_status", raise_for_status)
        return response

    return hook


def ado_session(pat: str | None) -> requests.Session:
    """A `requests.Session` authenticated for Azure DevOps whose rejected calls fail with an `AdoAuthError`
    (from `raise_for_status`) explaining which PAT / scope / login to fix, instead of a bare HTTP error."""
    session = requests.Session()
    session.headers.update(auth_header(pat))
    session.hooks["response"].append(_check_auth_hook(bool(pat)))
    return session
