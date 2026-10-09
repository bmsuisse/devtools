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


def test_table_that_is_entry_and_prefix(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[a]\nen = "top"\n\n[a.c]\nen = "y"\n')
    assert load_translations(p) == {"a": {"en": "top"}, "a.c": {"en": "y"}}


def test_duplicate_flattened_keys_raise(tmp_path):
    import pytest

    p = tmp_path / "t.toml"
    p.write_text('[a.b]\nen = "x"\n\n["a.b"]\nen = "y"\n')
    with pytest.raises(ValueError):
        load_translations(p)


def test_import_rejects_non_string(tmp_path):
    import pytest

    make_repo(tmp_path, "")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "en.json").write_text('{"k": null}')
    with pytest.raises(ValueError):
        run(load_config(tmp_path), import_json=True)


def test_check_detects_stale_and_missing_json(tmp_path):
    make_repo(tmp_path, '[A]\nen = "a"\n')
    assert (
        len(run(load_config(tmp_path), check=True).stale) == 4
    )  # nothing generated yet
    run(load_config(tmp_path))
    assert run(load_config(tmp_path), check=True).ok
    (tmp_path / "translations.toml").write_text('[A]\nen = "changed"\n')
    assert not run(load_config(tmp_path), check=True).ok


def test_scan_regex_variants(tmp_path):
    make_repo(tmp_path, "")
    (tmp_path / "src" / "a.vue").write_text(
        "this.$t('V1'); i18n.t('V2'); t('dyn.' + x); foo.t('NO')"
    )
    assert run(load_config(tmp_path), check=True).new_keys == ["V1", "V2"]


def test_add_key_cli(tmp_path, monkeypatch):
    make_repo(tmp_path, '[A]\nen = "a"\nde = "a"\n')
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(
        app, ["translate", "add", "NEW", "en=New", "de=Neu", "fr=Nouveau"]
    )
    assert r.exit_code == 0, r.output
    assert load_translations(tmp_path / "translations.toml")["NEW"] == {
        "en": "New",
        "de": "Neu",
        "fr": "Nouveau",
    }
    for lng, text in (
        ("en", "New"),
        ("de", "Neu"),
        ("fr", "Nouveau"),
        ("it", "Neu"),
    ):  # it falls back to de
        assert (
            json.loads((tmp_path / "out" / f"{lng}.json").read_text(encoding="utf-8"))[
                "NEW"
            ]
            == text
        )
    assert (
        runner.invoke(app, ["translate", "add", "NEW", "en=x"]).exit_code == 1
    )  # exists
    assert (
        runner.invoke(app, ["translate", "add", "NEW", "en=x", "--force"]).exit_code
        == 0
    )
    assert (
        runner.invoke(app, ["translate", "add", "K", "xx=x"]).exit_code == 1
    )  # unknown language
    assert runner.invoke(app, ["translate", "add", "K", "oops"]).exit_code == 2
    assert (
        runner.invoke(app, ["translate", "add", "K", "de=nur deutsch"]).exit_code == 1
    )  # en is required
    assert runner.invoke(app, ["translate", "add", "K", "en="]).exit_code == 1


def test_add_required_languages_configurable(tmp_path, monkeypatch):
    make_repo(tmp_path, "", PYPROJECT + 'required_languages = ["en", "de"]\n')
    monkeypatch.chdir(tmp_path)
    assert (
        runner.invoke(app, ["translate", "add", "K", "en=x"]).exit_code == 1
    )  # de now required too
    assert runner.invoke(app, ["translate", "add", "K", "en=x", "de=y"]).exit_code == 0
