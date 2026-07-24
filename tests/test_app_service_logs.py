import zipfile

from bmsdna.devtools import app_service_logs


def test_download_logs_omits_slot_flag_when_none(monkeypatch, tmp_path):
    captured = {}

    def fake_run_az(args):
        captured["args"] = args
        return ""

    monkeypatch.setattr(app_service_logs, "run_az", fake_run_az)
    app_service_logs.download_logs("my-app", "my-rg", None, tmp_path / "archive.zip")
    assert "--slot" not in captured["args"]


def test_download_logs_includes_slot_flag_when_given(monkeypatch, tmp_path):
    captured = {}

    def fake_run_az(args):
        captured["args"] = args
        return ""

    monkeypatch.setattr(app_service_logs, "run_az", fake_run_az)
    app_service_logs.download_logs("my-app", "my-rg", "test", tmp_path / "archive.zip")
    assert "--slot" in captured["args"]
    assert captured["args"][captured["args"].index("--slot") + 1] == "test"


def test_fetch_uses_production_label_when_slot_is_none(monkeypatch, tmp_path):
    def fake_download(webapp, resource_group, slot, archive):
        with zipfile.ZipFile(archive, "w"):
            pass

    monkeypatch.setattr(app_service_logs, "download_logs", fake_download)
    error_file = app_service_logs.fetch("my-app", "my-rg", None, tmp_path, keep_archive=True)
    assert error_file == tmp_path / "production_errors.log"
    assert (tmp_path / "production_logs.zip").exists()
