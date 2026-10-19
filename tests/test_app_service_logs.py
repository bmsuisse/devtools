import zipfile

import pytest

from bmsdna.devtools import app_service_logs, azure_arm


class FakeDump:
    status_code = 200
    reason = "OK"
    text = ""

    def __init__(self, data=b"zipbytes"):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size):
        yield self.data


def _setup(monkeypatch, dump=None):
    arm_calls = []
    get_calls = []

    def fake_arm(method, path, api_version, **kwargs):
        arm_calls.append((method, path, api_version))
        return {
            "properties": {
                "scmUri": "https://$user:pw@my-app.scm.azurewebsites.net",
                "publishingUserName": "$user",
                "publishingPassword": "pw",
            }
        }

    def fake_get(url, **kwargs):
        get_calls.append((url, kwargs))
        return dump or FakeDump()

    monkeypatch.setattr(azure_arm, "default_subscription_id", lambda: "sub-1")
    monkeypatch.setattr(azure_arm, "arm", fake_arm)
    monkeypatch.setattr(app_service_logs.requests, "get", fake_get)
    return arm_calls, get_calls


def test_download_logs_production_uses_site_credentials_path(monkeypatch, tmp_path):
    arm_calls, get_calls = _setup(monkeypatch)
    archive = tmp_path / "archive.zip"

    app_service_logs.download_logs("my-app", "my-rg", None, archive)

    assert arm_calls == [
        (
            "POST",
            "/subscriptions/sub-1/resourceGroups/my-rg/providers/Microsoft.Web/sites/my-app/config/publishingcredentials/list",
            "2022-03-01",
        )
    ]
    url, kwargs = get_calls[0]
    assert url == "https://my-app.scm.azurewebsites.net/api/dump"
    assert kwargs["auth"] == ("$user", "pw")
    assert archive.read_bytes() == b"zipbytes"


def test_download_logs_includes_slot_in_path_when_given(monkeypatch, tmp_path):
    arm_calls, _ = _setup(monkeypatch)

    app_service_logs.download_logs("my-app", "my-rg", "test", tmp_path / "archive.zip")

    assert "/sites/my-app/slots/test/config/publishingcredentials/list" in arm_calls[0][1]


def test_download_logs_exits_on_arm_error(monkeypatch, tmp_path):
    _setup(monkeypatch)

    def boom(*a, **k):
        raise azure_arm.AzureApiError("POST x failed (HTTP 403): no access")

    monkeypatch.setattr(azure_arm, "arm", boom)

    with pytest.raises(SystemExit) as exc_info:
        app_service_logs.download_logs("my-app", "my-rg", None, tmp_path / "archive.zip")

    assert "no access" in str(exc_info.value)


def test_download_logs_exits_on_kudu_error(monkeypatch, tmp_path):
    dump = FakeDump()
    dump.status_code = 401
    dump.text = "Unauthorized"
    _setup(monkeypatch, dump)

    with pytest.raises(SystemExit) as exc_info:
        app_service_logs.download_logs("my-app", "my-rg", None, tmp_path / "archive.zip")

    assert "401" in str(exc_info.value)


def test_fetch_uses_production_label_when_slot_is_none(monkeypatch, tmp_path):
    def fake_download(webapp, resource_group, slot, archive):
        with zipfile.ZipFile(archive, "w"):
            pass

    monkeypatch.setattr(app_service_logs, "download_logs", fake_download)
    error_file = app_service_logs.fetch("my-app", "my-rg", None, tmp_path, keep_archive=True)
    assert error_file == tmp_path / "production_errors.log"
    assert (tmp_path / "production_logs.zip").exists()


def test_extract_errors_keeps_only_error_lines(tmp_path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("LogFiles/app.log", "all fine\nERROR boom\nGET /x 500\n")
    out = tmp_path / "errors.log"

    assert app_service_logs.extract_errors(archive, out) == 2
    assert out.read_text() == "LogFiles/app.log: ERROR boom\nLogFiles/app.log: GET /x 500\n"


def test_download_logs_leaves_no_partial_file_when_the_stream_breaks(monkeypatch, tmp_path):
    class Broken(FakeDump):
        def iter_content(self, chunk_size):
            yield b"half"
            raise app_service_logs.requests.ConnectionError("dropped")

    _setup(monkeypatch, Broken())
    archive = tmp_path / "archive.zip"

    with pytest.raises(SystemExit, match="dropped"):
        app_service_logs.download_logs("my-app", "my-rg", None, archive)

    assert list(tmp_path.iterdir()) == []


def test_download_logs_keeps_an_existing_archive_when_the_stream_breaks(monkeypatch, tmp_path):
    class Broken(FakeDump):
        def iter_content(self, chunk_size):
            raise app_service_logs.requests.ConnectionError("dropped")

    _setup(monkeypatch, Broken())
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"previous")

    with pytest.raises(SystemExit):
        app_service_logs.download_logs("my-app", "my-rg", None, archive)

    assert archive.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["archive.zip"]
