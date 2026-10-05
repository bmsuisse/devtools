import json

from typer.testing import CliRunner

from bmsdna.devtools.cli import app
from bmsdna.devtools.translate import (
    build_language,
    load_config,
    load_translations,
    run,
)

runner = CliRunner()

PYPROJECT = """
[tool.bdt.translate]
output = ["out"]
scan = ["src"]
scan_jinja = ["print"]
"""


def make_repo(tmp_path, toml, pyproject=PYPROJECT):
    (tmp_path / "pyproject.toml").write_text(pyproject)
    (tmp_path / "translations.toml").write_text(toml, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "print").mkdir()
    return tmp_path


def test_generates_json_with_fallbacks(tmp_path):
    make_repo(
        tmp_path,
        '[ADD]\nen = "Add"\nde = "Hinzufügen"\n\n[ONLY_EN]\nen = "x"\n\n[SRV]\nen = "s"\nserver_only = true\n',
    )
    res = run(load_config(tmp_path))
    assert res.ok and len(res.written) == 4
    de = json.loads((tmp_path / "out" / "de.json").read_text(encoding="utf-8"))
    fr = json.loads((tmp_path / "out" / "fr.json").read_text(encoding="utf-8"))
    assert de == {"ADD": "Hinzufügen", "ONLY_EN": "x"}  # server_only excluded
    assert fr["ADD"] == "Hinzufügen"  # fr -> de fallback
    assert res.incomplete["ONLY_EN"] == ["de", "fr", "it"]


def test_new_keys_from_code_are_added_and_block_generation(tmp_path):
    make_repo(tmp_path, '[A]\nen = "a"\n')
    (tmp_path / "src" / "x.tsx").write_text(
        "t('A'); t(\"NEW_KEY\"); foo.t('IGNORED'); $t('VUE_KEY')"
    )
    (tmp_path / "print" / "r.jinja2").write_text("{{ 'JKEY' | tr }}")
    res = run(load_config(tmp_path))
    assert res.new_keys == ["JKEY", "NEW_KEY", "VUE_KEY"]
    assert not (tmp_path / "out").exists()
    assert load_translations(tmp_path / "translations.toml")["NEW_KEY"] == {
        "en": "NEW_KEY"
    }


def test_check_does_not_write(tmp_path):
    make_repo(tmp_path, '[A]\nen = "a"\n')
    (tmp_path / "src" / "x.ts").write_text("t('B')")
    before = (tmp_path / "translations.toml").read_text()
    res = run(load_config(tmp_path), check=True)
    assert res.new_keys == ["B"]
    assert (tmp_path / "translations.toml").read_text() == before
    assert not (tmp_path / "out").exists()


def test_import_existing_json_nested(tmp_path):
    make_repo(tmp_path, "", PYPROJECT + "nested = true\n")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "en.json").write_text('{"a": {"b": "B"}}')
    (tmp_path / "out" / "de.json").write_text('{"a": {"b": "Bd"}}')
    res = run(load_config(tmp_path), import_json=True)
    assert res.imported == 2
    assert load_translations(tmp_path / "translations.toml")["a.b"] == {
        "en": "B",
        "de": "Bd",
    }
    assert json.loads((tmp_path / "out" / "de.json").read_text()) == {"a": {"b": "Bd"}}


def test_dotted_table_headers_are_flattened(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[a.b]\nen = "x"\n\n["c.d"]\nen = "y"\n')
    assert load_translations(p) == {"a.b": {"en": "x"}, "c.d": {"en": "y"}}


def test_nested_key_conflict():
    import pytest

    with pytest.raises(ValueError):
        build_language({"a": {"en": "x"}, "a.b": {"en": "y"}}, "en", True)


def test_cli(tmp_path, monkeypatch):
    make_repo(tmp_path, '[A]\nen = "a"\n')
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["translate"]).exit_code == 0
    assert (tmp_path / "out" / "en.json").is_file()
    (tmp_path / "src" / "x.ts").write_text("t('B')")
    assert runner.invoke(app, ["translate", "--check"]).exit_code == 1
