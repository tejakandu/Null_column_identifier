"""Clients must never start gcloud (on Windows each call flashes a console)."""

import json
import subprocess
import threading

import pytest

import nci.core as core


@pytest.fixture
def adc(tmp_path, monkeypatch):
    cfg = tmp_path / "gcloud"
    cfg.mkdir()
    (cfg / "application_default_credentials.json").write_text(json.dumps({
        "type": "authorized_user", "client_id": "x.apps.googleusercontent.com",
        "client_secret": "s", "refresh_token": "r"}))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(cfg))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(core, "_creds_cache", {})
    started = []
    original = subprocess.Popen.__init__

    def spy(self, *args, **kwargs):
        started.append(args[0] if args else kwargs.get("args"))
        original(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", spy)
    return started


def test_no_subprocess_for_many_threads_and_no_project(adc):
    pool = core.ClientPool(core.make_client)
    threads = [threading.Thread(target=lambda: pool.get("my-project", "")) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    client = pool.get("", "")  # listing projects: no project chosen yet
    assert client.project == core.PLACEHOLDER_PROJECT
    assert pool.get("other", "").project == "other"
    assert adc == []


def test_credentials_loaded_once(adc, monkeypatch):
    calls = []
    real = core._load_credentials
    monkeypatch.setattr(core, "_load_credentials", lambda k: calls.append(k) or real(k))
    for _ in range(5):
        core.make_client("p")
    assert calls == [""]


def test_missing_login_gives_friendly_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "none"))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(core, "_creds_cache", {})
    import google.auth

    def fail(**kw):
        raise google.auth.exceptions.DefaultCredentialsError("Your default credentials were not found.")

    monkeypatch.setattr(google.auth, "default", fail)
    with pytest.raises(Exception) as err:
        core.make_client("p")
    assert core.friendly_error(err.value).startswith("No Google login found")


def test_windows_child_processes_get_no_window(monkeypatch):
    seen = {}

    def record(self, *args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", record)
    monkeypatch.setattr(core.os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    core.hide_console_windows()
    core.hide_console_windows()  # idempotent
    subprocess.Popen.__init__(object.__new__(subprocess.Popen), ["gcloud"], creationflags=0x10)
    assert seen["creationflags"] == 0x08000010
