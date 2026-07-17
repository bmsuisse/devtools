import pytest

from bmsdna.devtools.gitrepo import AdoRemote, NotAzureDevOpsRemoteError, parse_ado_remote


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
