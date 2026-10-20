"""Parser for the `gh`-style flags `bdt pr create` / `bdt issue create` accept after `--` on GitHub.

There is no `gh` to forward them to any more, so each supported flag is mapped to an API field by the
caller; anything not in the caller's spec exits with the list of what is supported.
"""

from __future__ import annotations

import sys
import webbrowser
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Flag:
    key: str
    kind: str = "value"  # "value" | "list" (repeatable, comma-separated) | "bool"


_FALSE = {"false", "0", "no"}


def parse_flags(args: list[str], spec: dict[str, Flag], command: str) -> dict[str, Any]:
    """`--opt value` / `--opt=value` / `-o value` / `-o=value` (bool flags take no value, or an explicit
    `=true|false`) into `{key: value}`; list flags accumulate. Unknown flags exit naming the supported ones."""
    out: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        name, eq, inline = arg.partition("=")
        flag = spec.get(name)
        if flag is None:
            supported = ", ".join(sorted(n for n in spec if n.startswith("--")))
            sys.exit(f"unsupported argument for `{command}`: {arg!r} (supported: {supported})")
        if flag.kind == "bool":
            out[flag.key] = inline.lower() not in _FALSE if eq else True
            continue
        if eq:
            value = inline
        elif i < len(args):
            value = args[i]
            i += 1
        else:
            sys.exit(f"missing value for {name} in `{command}`")
        if flag.kind == "list":
            out.setdefault(flag.key, []).extend(v.strip() for v in value.split(",") if v.strip())
        else:
            out[flag.key] = value
    return out


def read_body_file(path: str, flag: str = "--body-file") -> str:
    """The text of a `--body-file`-style flag: a file, or stdin for `-`."""
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        sys.exit(f"cannot read {flag} {path!r}: {e}")


def open_in_browser(url: str | None) -> None:
    """`--web`: best-effort, a failure to open a browser never fails the command."""
    if not url:
        return
    try:
        webbrowser.open(url)
    except webbrowser.Error:
        pass
