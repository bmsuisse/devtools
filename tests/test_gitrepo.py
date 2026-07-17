import pytest

from bmsdna.devtools.gitrepo import (
    AdoRemote,
    GitHubRemote,
    NotAzureDevOpsRemoteError,
    NotGitHubRemoteError,
    UnknownRemoteError,
    parse_ado_remote,
    parse_github_remote,
    parse_remote,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@ssh.dev.azure.com:v3/bmeurope/BMS%20-%20Data/MDMApp", AdoRemote("bmeurope", "BMS - Data", "MDMApp")),
        ("https://dev.azure.com/bmeurope/BMS%20-%20CCMT2/_git/ccmt2", AdoRemote("bmeurope", "BMS - CCMT2", "ccmt2")),
        ("https://user@dev.azure.com/bmeurope/BMS%20%E2%80%93%20MyPage/_git/onesales", AdoRemote("bmeurope", "BMS – MyPage", "onesales")),
        ("https://bmeurope.visualstudio.com/BMS%20-%20Data/_git/MDMApp", AdoRemote("bmeurope", "BMS - Data", "MDMApp")),
    ],
)
def test_parse_ado_remote(url: str, expected: AdoRemote) -> None:
    assert parse_ado_remote(url) == expected


def test_parse_ado_remote_rejects_non_ado_url() -> None:
    with pytest.raises(NotAzureDevOpsRemoteError):
        parse_ado_remote("git@github.com:bmsuisse/devtools.git")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:bmsuisse/devtools.git", GitHubRemote("bmsuisse", "devtools")),
        ("git@github.com:bmsuisse/devtools", GitHubRemote("bmsuisse", "devtools")),
        ("https://github.com/bmsuisse/devtools.git", GitHubRemote("bmsuisse", "devtools")),
        ("https://github.com/bmsuisse/devtools", GitHubRemote("bmsuisse", "devtools")),
        ("ssh://git@github.com/bmsuisse/devtools.git", GitHubRemote("bmsuisse", "devtools")),
    ],
)
def test_parse_github_remote(url: str, expected: GitHubRemote) -> None:
    assert parse_github_remote(url) == expected


def test_parse_github_remote_rejects_non_github_url() -> None:
    with pytest.raises(NotGitHubRemoteError):
        parse_github_remote("git@ssh.dev.azure.com:v3/bmeurope/BMS%20-%20Data/MDMApp")


def test_parse_remote_dispatches_to_github() -> None:
    assert parse_remote("git@github.com:bmsuisse/devtools.git") == GitHubRemote("bmsuisse", "devtools")


def test_parse_remote_dispatches_to_ado() -> None:
    assert parse_remote("git@ssh.dev.azure.com:v3/bmeurope/BMS%20-%20Data/MDMApp") == AdoRemote(
        "bmeurope", "BMS - Data", "MDMApp"
    )


def test_parse_remote_rejects_unknown_host() -> None:
    with pytest.raises(UnknownRemoteError):
        parse_remote("git@gitlab.com:some/repo.git")
