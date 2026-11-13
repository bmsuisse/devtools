import pytest

from bmsdna.devtools.deps.versions import (
    VersionError,
    normalize_name,
    npm_range_satisfied,
    parse_maven,
    parse_semver,
    pypi_specifier_satisfied,
    spec_matches,
)


def test_pypi_names_are_normalised_npm_names_are_not() -> None:
    assert normalize_name("PyPI", "Foo_Bar.baz") == "foo-bar-baz"
    assert normalize_name("npm", "@Scope/Pkg_x") == "@Scope/Pkg_x"


@pytest.mark.parametrize(
    ("ecosystem", "spec", "version", "expected"),
    [
        ("npm", "<4.17.21", "4.17.15", True),
        ("npm", "<4.17.21", "4.17.21", False),
        ("npm", ">=1.0.0,<2.0.0", "2.0.0", False),
        ("npm", ">= 1.0.0 < 2.0.0", "1.5.0", True),
        ("npm", "<1.0.0", "1.0.0-rc.1", True),
        ("PyPI", ">=2.0,<2.32.4", "2.19.1", True),
        ("PyPI", "==2.19.1", "2.19.1", True),
        ("PyPI", "!=2.19.1", "2.19.1", False),
        ("Maven", "<4.12.1", "4.12.0", True),
        ("Maven", ">=1.0,<2", "2.0-rc1", True),
        ("Maven", "<2025.10.00", "2025.09.00", True),
        ("Maven", "<1.10", "1.10.0", False),
    ],
)
def test_spec_matches(ecosystem: str, spec: str, version: str, expected: bool) -> None:
    assert spec_matches(ecosystem, spec, version) is expected  # type: ignore[arg-type]


def test_semver_prerelease_ordering() -> None:
    assert (
        parse_semver("1.0.0-alpha.1")
        < parse_semver("1.0.0-alpha.beta")
        < parse_semver("1.0.0-rc.1")
        < parse_semver("1.0.0")
    )
    assert parse_semver("1.0.0-2") < parse_semver("1.0.0-10")


@pytest.mark.parametrize(
    ("declared", "version", "expected"),
    [
        ("^4", "4.13.18", True),
        ("^4", "5.0.0", False),
        ("~1.2", "1.2.9", True),
        ("~1.2", "1.3.0", False),
        ("^0.2.3", "0.2.9", True),
        ("^0.2.3", "0.3.0", False),
        ("1.x", "1.9.0", True),
        (">=1 <3 || ^5", "5.1.0", True),
        (">=1 <3", "3.0.0", False),
        ("*", "9.9.9", True),
        ("4.17.15", "4.18.0", False),
    ],
)
def test_npm_ranges(declared: str, version: str, expected: bool) -> None:
    assert npm_range_satisfied(declared, version) is expected


@pytest.mark.parametrize("declared", ["latest", "workspace:*", "github:a/b", "npm:other@1", "1.0.0 - 2.0.0"])
def test_npm_specs_that_are_not_ranges_are_unknown_not_false(declared: str) -> None:
    with pytest.raises(VersionError):
        npm_range_satisfied(declared, "1.0.0")


def test_pypi_specifier() -> None:
    assert pypi_specifier_satisfied(">=2.19,<3", "2.34.2")
    assert not pypi_specifier_satisfied(">=24.1.0,<25", "25.0.0")
    with pytest.raises(VersionError):
        pypi_specifier_satisfied("not a specifier", "1.0")


@pytest.mark.parametrize(
    ("declared", "version", "expected"),
    [
        ("^1.2.3", "1.5.0-rc.1", False),  # node-semver: a prerelease needs a comparator naming one of the same x.y.z
        ("^1.5.0-rc.0", "1.5.0-rc.1", True),
        (">=1.5.0-rc.0 <2", "1.6.0-rc.1", False),
    ],
)
def test_npm_prereleases_only_satisfy_ranges_that_name_a_prerelease_of_the_same_version(
    declared: str, version: str, expected: bool
) -> None:
    assert npm_range_satisfied(declared, version) is expected


def test_maven_ordering_follows_comparable_version() -> None:
    assert parse_maven("1.0") == parse_maven("1.0.0")
    assert hash(parse_maven("1.0")) == hash(parse_maven("1.0.0"))
    assert (
        parse_maven("1.0-alpha-1")
        < parse_maven("1.0-beta")
        < parse_maven("1.0-rc1")
        < parse_maven("1.0")
        < parse_maven("1.0-sp1")
        < parse_maven("1.0.1")
    )
    assert parse_maven("1.9.0") < parse_maven("1.10.0")  # numeric, not lexical
    assert parse_maven("4.12.0") < parse_maven("5.0.0-alpha.3")


def test_a_maven_version_that_is_not_one_is_unknown() -> None:
    with pytest.raises(VersionError):
        parse_maven("[1.0,2.0)")
