"""Download an Azure App Service log archive and filter it for errors/warnings.

Pulls the log zip from the app's Kudu `/api/dump` endpoint (authenticated with its
publishing credentials, fetched via ARM), unpacks it in memory, and
writes every line matching common error/warning markers to a filtered file.
The webapp/resource-group/slot for a given `--env` come from the consuming
repo's pyproject.toml (see env_config.py) rather than being baked in here.
"""

from __future__ import annotations

import pathlib
import re
import sys
import zipfile
from urllib.parse import quote, urlparse

import requests

from . import azure_arm

# Matches common error/warning markers across granian, uvicorn, and python tracebacks.
ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|WARNING|Traceback|Exception|\b[45]\d\d\b|FAILED|FATAL)\b")


WEBAPP_API_VERSION = "2022-03-01"
DOWNLOAD_TIMEOUT_SECS = 300.0


def download_logs(webapp: str, resource_group: str, slot: str | None, archive: pathlib.Path) -> None:
    print(f"Downloading logs for {webapp}/{slot or '(production)'}...", flush=True)
    try:
        sub = azure_arm.default_subscription_id()
        site = (
            f"/subscriptions/{sub}/resourceGroups/{quote(resource_group)}"
            f"/providers/Microsoft.Web/sites/{quote(webapp)}"
        )
        if slot:
            site += f"/slots/{quote(slot)}"
        creds = azure_arm.arm("POST", f"{site}/config/publishingcredentials/list", WEBAPP_API_VERSION)["properties"]
        scm_host = urlparse(creds["scmUri"]).hostname
        with requests.get(
            f"https://{scm_host}/api/dump",
            auth=(creds["publishingUserName"], creds["publishingPassword"]),
            stream=True,
            timeout=DOWNLOAD_TIMEOUT_SECS,
        ) as r:
            if r.status_code >= 400:
                sys.exit(f"Log download failed (HTTP {r.status_code}): {r.text.strip() or r.reason}")
            with archive.open("wb") as out:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    out.write(chunk)
    except azure_arm.AzureApiError as e:
        sys.exit(str(e))
    except requests.RequestException as e:
        sys.exit(f"Log download failed: {e}")


def extract_errors(archive: pathlib.Path, error_file: pathlib.Path) -> int:
    """Unzip the archive and write all matching lines to error_file. Returns count."""
    count = 0
    with zipfile.ZipFile(archive) as zf, error_file.open("w") as out:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            with zf.open(member) as fh:
                for raw in fh:
                    line = raw.decode("utf-8", "replace")
                    if ERROR_RE.search(line):
                        out.write(f"{member}: {line}")
                        count += 1
    return count


def fetch(
    webapp: str,
    resource_group: str,
    slot: str | None,
    out_dir: pathlib.Path,
    keep_archive: bool = False,
) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    label = slot or "production"
    archive = out_dir / f"{label}_logs.zip"
    error_file = out_dir / f"{label}_errors.log"

    download_logs(webapp, resource_group, slot, archive)
    count = extract_errors(archive, error_file)

    if not keep_archive:
        archive.unlink()

    print(f"✓ {count} error/warning lines → {error_file}")
    return error_file
