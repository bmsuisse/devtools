"""`bdt translate`: one `translations.toml` -> generated `<lng>.json` files per project.

Replaces the per-repo `transl_sync.py` scripts (CCMT, OneSales, MDMApp's akeneo editor).
`translations.toml` is the single source of truth; the generated JSON files are build
artifacts and should be git-ignored.

Configuration lives in the consuming repo's pyproject.toml::

    [tool.bdt.translate]
    file = "translations.toml"               # default
    languages = ["en", "de", "fr", "it"]     # default
    output = ["frontend/src/assets/i18n"]    # dir(s) that receive <lng>.json
    nested = false                           # true: split keys on "." into nested JSON objects
    scan = ["frontend/src"]                  # t("KEY") usages in .ts/.tsx/.js/.jsx/.vue
    scan_jinja = ["backend/print"]           # "KEY" | tr usages in .jinja2/.j2/.html
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from .bdt_config import find_pyproject, load_bdt_table

SCAN_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".vue"}
JINJA_SUFFIXES = {".jinja2", ".j2", ".html"}
SKIP_DIRS = {
    "node_modules",
    ".git",
    ".venv",
    "dist",
    "build",
    "generated",
    "__pycache__",
}
REGEX_T = re.compile(r"(?<![.\w$])\$?t\([\"']([a-zA-Z0-9\-._]+)[\"']")
REGEX_JINJA = re.compile(r"[\"']([a-zA-Z0-9\-._]+)[\"']\s*\|\s*tr\b")

Translations = dict[str, dict[str, str]]


@dataclass
class Config:
    root: Path
    file: Path
    languages: list[str] = field(default_factory=lambda: ["en", "de", "fr", "it"])
    output: list[Path] = field(default_factory=list)
    nested: bool = False
    scan: list[Path] = field(default_factory=list)
    scan_jinja: list[Path] = field(default_factory=list)


def load_config(start: Path | None = None) -> Config:
    pyproject = find_pyproject(start)
    root = pyproject.parent if pyproject else (start or Path.cwd())
    table = load_bdt_table("translate", start)

    def paths(key: str) -> list[Path]:
        value = table.get(key, [])
        return [root / p for p in ([value] if isinstance(value, str) else value)]

    return Config(
        root=root,
        file=root / table.get("file", "translations.toml"),
        languages=list(table.get("languages", ["en", "de", "fr", "it"])),
        output=paths("output"),
        nested=bool(table.get("nested", False)),
        scan=paths("scan"),
        scan_jinja=paths("scan_jinja"),
    )


def _is_entry(d: dict) -> bool:
    """A translation entry maps language codes (and flags like `server_only`) to scalars."""
    return all(not isinstance(v, dict) for v in d.values())


def _flatten_toml(data: dict, prefix: str = "") -> Translations:
    # `[a.b]` in TOML parses as {"a": {"b": {...}}}; fold that back into the key "a.b".
    out: Translations = {}
    for k, v in data.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            if _is_entry(v):
                out[key] = v
            else:
                out.update(_flatten_toml(v, key))
    return out


def load_translations(path: Path) -> Translations:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return _flatten_toml(tomllib.load(f))


def save_translations(path: Path, transls: Translations) -> None:
    path.write_text(tomli_w.dumps(transls), encoding="utf-8")


def _flatten_json(obj: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten_json(v, key))
        else:
            out[key] = v
    return out


def import_existing(cfg: Config, transls: Translations) -> int:
    """Merge already existing `<lng>.json` files into `transls` (one-time migration).

    Existing toml values win. Returns the number of values added."""
    added = 0
    for out_dir in cfg.output:
        for lng in cfg.languages:
            p = out_dir / f"{lng}.json"
            if not p.is_file():
                continue
            for key, value in _flatten_json(
                json.loads(p.read_text(encoding="utf-8-sig"))
            ).items():
                entry = transls.setdefault(key, {})
                if lng not in entry:
                    entry[lng] = value
                    added += 1
    return added


def _iter_files(roots: list[Path], suffixes: set[str]):
    for root in roots:
        files = (
            [root]
            if root.is_file()
            else sorted(root.rglob("*"))
            if root.is_dir()
            else []
        )
        for f in files:
            if (
                f.is_file()
                and f.suffix in suffixes
                and not SKIP_DIRS.intersection(f.relative_to(root).parts[:-1])
            ):
                yield f


def find_used_keys(cfg: Config) -> set[str]:
    keys: set[str] = set()
    for roots, suffixes, regex in (
        (cfg.scan, SCAN_SUFFIXES, REGEX_T),
        (cfg.scan_jinja, JINJA_SUFFIXES, REGEX_JINJA),
    ):
        for f in _iter_files(roots, suffixes):
            keys.update(regex.findall(f.read_text(encoding="utf-8", errors="replace")))
    return keys


def resolve(entry: dict, key: str, lng: str) -> str:
    """Value for `lng`, falling back to de, then en, then the key itself."""
    for code in (lng, "de", "en"):
        if code in entry:
            return entry[code]
    return key


def build_language(transls: Translations, lng: str, nested: bool) -> dict:
    flat = {
        k: resolve(v, k, lng) for k, v in transls.items() if not v.get("server_only")
    }
    if not nested:
        return flat
    tree: dict = {}
    for key, value in sorted(flat.items()):
        node = tree
        *parents, leaf = key.split(".")
        for part in parents:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(
                    f"Key conflict: {key!r} needs {part!r} to be an object, but it is a string"
                )
            node = child
        if isinstance(node.get(leaf), dict):
            raise ValueError(f"Key conflict: {key!r} is also a prefix of other keys")
        node[leaf] = value
    return tree


@dataclass
class Result:
    new_keys: list[str] = field(default_factory=list)
    incomplete: dict[str, list[str]] = field(
        default_factory=dict
    )  # key -> missing languages
    imported: int = 0
    written: list[Path] = field(default_factory=list)
    toml_changed: bool = False

    @property
    def ok(self) -> bool:
        return not self.new_keys


def run(cfg: Config, *, check: bool = False, import_json: bool = False) -> Result:
    res = Result()
    original = load_translations(cfg.file)
    transls: Translations = {k: dict(v) for k, v in original.items()}

    if import_json:
        res.imported = import_existing(cfg, transls)

    used = find_used_keys(cfg)
    res.new_keys = sorted(k for k in used if k not in transls)
    for k in res.new_keys:
        transls[k] = {"en": k}
    res.incomplete = {
        k: missing
        for k, v in transls.items()
        if not v.get("server_only")
        and (missing := [lng for lng in cfg.languages if lng not in v])
    }

    res.toml_changed = transls != original
    if check:
        return res
    if res.toml_changed:
        save_translations(cfg.file, transls)
    if res.new_keys:
        return res  # placeholders were added; fill in the real translations first, then rerun
    for out_dir in cfg.output:
        out_dir.mkdir(parents=True, exist_ok=True)
        for lng in cfg.languages:
            p = out_dir / f"{lng}.json"
            p.write_text(
                json.dumps(
                    build_language(transls, lng, cfg.nested),
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            res.written.append(p)
    return res
