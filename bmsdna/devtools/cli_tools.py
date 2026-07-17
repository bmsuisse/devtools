"""Locating required external CLIs (az, gh) with clear errors when missing.

Uses `shutil.which` rather than a bare command name so Windows .cmd/.bat/.exe
shims (e.g. az.cmd from the MSI installer) resolve correctly via PATHEXT —
the same lookup `where`/`Get-Command` would do — instead of guessing an
extension or relying on shell=True (which also avoids any shell-quoting
concerns for arguments that come from user input, e.g. PR titles).
"""

from __future__ import annotations

import shutil
import sys

AZ_INSTALL_HINT = "Install the Azure CLI: https://learn.microsoft.com/cli/azure/install-azure-cli"
GH_INSTALL_HINT = "Install the GitHub CLI: https://cli.github.com"


def require_tool(name: str, install_hint: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"'{name}' is required for this command but wasn't found on PATH.\n{install_hint}")
    return path


def require_az() -> str:
    return require_tool("az", AZ_INSTALL_HINT)


def require_gh() -> str:
    return require_tool("gh", GH_INSTALL_HINT)
