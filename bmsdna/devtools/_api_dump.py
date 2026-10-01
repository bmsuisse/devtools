"""Standalone (stdlib-only) helper of `bdt lint-api-usage`: list the documented HTTP operations of a
FastAPI-like app, recursing into mounted sub-apps.

It is executed in the *target repo's* interpreter via `python -c <this file's source> module:attr out.json`
and must therefore never import `bmsdna.devtools`: the working directory is first on `sys.path` there, and a repo may
ship its own top-level `bmsdna` package (CCMT2 does) that would shadow ours.
"""

import importlib
import json
import sys

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")


def collect(app, mount=""):
    """Operations of `app` and, recursively, of every mounted sub-app, as dicts. Uses only `app.openapi()` and the
    `.path`/`.app` attributes of mount routes -- not FastAPI internals, which change between releases (FastAPI 0.141 wraps
    included routers in lazy objects that `app.routes` no longer lists as plain routes)."""
    openapi = getattr(app, "openapi", None)
    if not callable(openapi):
        return []
    ops = []
    for path, item in (openapi().get("paths") or {}).items():
        for method, op in item.items():
            if method in HTTP_METHODS and isinstance(op, dict):
                ops.append({"method": method.upper(), "path": mount + path, "tags": list(op.get("tags") or []), "mount": mount})
    for route in getattr(app, "routes", None) or []:
        sub = getattr(route, "app", None)
        if sub is not None and callable(getattr(sub, "openapi", None)) and isinstance(getattr(route, "path", None), str):
            ops.extend(collect(sub, mount + route.path.rstrip("/")))
    return ops


def main(argv):
    module_name, _, attr = argv[0].partition(":")
    app = getattr(importlib.import_module(module_name), attr)
    with open(argv[1], "w", encoding="utf-8") as out:
        json.dump(collect(app), out)


if __name__ == "__main__":
    main(sys.argv[1:])
