"""Download an Azure App Service log archive and filter it for errors/warnings.

Pulls the log zip via `az webapp log download`, unpacks it in memory, and
writes every line matching common error/warning markers to a filtered file.
The webapp/resource-group/slot for a given `--env` come from the consuming
repo's pyproject.toml (see env_config.py) rather than being baked in here.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import zipfile

from .cli_tools import require_az

# Matches common error/warning markers across granian, uvicorn, and python tracebacks.
ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|WARNING|Traceback|Exception|\b[45]\d\d\b|FAILED|FATAL)\b")


def run_az(args: list[str]) -> str:
    az = require_az()
    proc = subprocess.run([az, *args], capture_output=True, encoding="utf-8")
    if proc.returncode != 0:
        sys.exit(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


def download_logs(webapp: str, resource_group: str, slot: str | None, archive: pathlib.Path) -> None:
    print(f"Downloading logs for {webapp}/{slot or '(production)'}...", flush=True)
    run_az([
        "webapp", "log", "download",
        "--name", webapp,
        "--resource-group", resource_group,
        *(["--slot", slot] if slot else []),
        "--log-file", str(archive),
    ])


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
