"""`bdt ci warnings`: run a command and report the warnings it only prints (vite, pytest) to the CI system."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping

from bmsdna.devtools import ci_plan_env
from bmsdna.devtools.build_warnings import tool_warnings


def _github(message: str) -> str:
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning::{escaped}"


def announce(warnings: list[str], env: Mapping[str, str]) -> None:
    """Azure Pipelines and GitHub Actions count them (`bdt pr status` shows how many); elsewhere they are plain lines."""
    for message in warnings:
        if env.get("TF_BUILD"):
            ci_plan_env.log(True, "warning", message)
        elif env.get("GITHUB_ACTIONS"):
            print(_github(message))
        else:
            print(f"warning: {message}")


def run(command: list[str], env: Mapping[str, str] | None = None) -> int:
    """Run `command` with its output passed through as it comes, then announce its warnings; its exit code is returned."""
    env = os.environ if env is None else env
    executable = shutil.which(command[0])
    if executable is None:
        print(f"error: {command[0]}: command not found", file=sys.stderr)
        return 127
    output: list[str] = []
    with subprocess.Popen(
        [executable, *command[1:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            output.append(line)
    announce(tool_warnings("".join(output)), env)
    return process.returncode
