"""Opt-in management over real pinned TLS, without any model/network downloads."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from services import hf_access
from services.remote_asr import settings as remote_settings
from services.remote_asr.client import (
    PairingResult,
    RemoteConnection,
    RemoteEngineError,
)
from services.remote_asr.model_management import HostModelManager
from services.remote_asr.service import RemoteEngineService
from services.settings import HuggingFaceAccessPolicy, SettingsKey, settings_manager
from tests.test_remote_engine import FakeEngine, _connect, _wait_for


@pytest.fixture
def downloads(monkeypatch):
    state = SimpleNamespace(cached=set(), calls=[], entered=threading.Event(), release=threading.Event())
    state.release.set()
    coordinator = hf_access.HuggingFaceAccessCoordinator()
    monkeypatch.setattr(hf_access, "hf_access_coordinator", coordinator)
    monkeypatch.setattr(hf_access, "is_model_cached", lambda model: model in state.cached)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    coordinator.set_policy(HuggingFaceAccessPolicy.ALWAYS)

    def download(model, progress_callback):
        state.calls.append(model)
        progress_callback(25, 100)
        state.entered.set()
        assert state.release.wait(5)
        state.cached.add(model)
        progress_callback(100, 100)
        return "not-exposed-to-the-client"

    monkeypatch.setattr(hf_access, "download_model_files", download)
    state.coordinator = coordinator
    yield state
    state.release.set()
    assert _wait_for(lambda: not coordinator.requests_in_flight)


@pytest.fixture
def managed(tmp_path, monkeypatch, downloads):
    from services import components
    from services.local_asr import catalog
    from services.remote_asr import tailscale

    monkeypatch.setattr(components, "is_installed", lambda component: False)
    monkeypatch.setattr(catalog, "resolve_runtime", lambda family, device: ("missing-runtime", "cpu"))
    monkeypatch.setattr(tailscale, "status", lambda: tailscale.TailscaleStatus("not_installed"))
    engine = FakeEngine()

    def switch(family, model, name, device=None):
        engine.family, engine.model = family, model

    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "identity"), switch_engine=switch)
    monkeypatch.setattr(service, "_engine", lambda: engine)
    monkeypatch.setattr(service, "_host_addresses", lambda: [])
    monkeypatch.setattr(service, "host_models", lambda: [
        {"family": "local_whisper", "model": model, "label": f"Whisper {model}"}
        for model in downloads.cached
    ])
    host = service._ensure_host()
    host.start(port=0, bind="127.0.0.1")
    device, token = host.registry.add("laptop")
    pairing = PairingResult(token, device["id"], host.host_name, host.identity.fingerprint)
    remote_settings.save_client_pairing("127.0.0.1", host.port, pairing)
    state = SimpleNamespace(service=service, host=host, engine=engine, pairing=pairing)
    yield state
    service.shutdown()
    assert _wait_for(lambda: service._model_manager.job().get("state") != "downloading")


@pytest.mark.parametrize("value", [None, False, "true", "false", 1, {}, []])
def test_management_requires_explicit_boolean_opt_in(value):
    assert not remote_settings.host_model_management({SettingsKey.REMOTE_HOST_MODEL_MANAGEMENT: value})
    assert remote_settings.host_model_management({SettingsKey.REMOTE_HOST_MODEL_MANAGEMENT: True})


def test_permission_is_rechecked_on_existing_connections(managed):
    connection = _connect(managed.host, managed.pairing)
    try:
        assert connection.ready["capabilities"]["model_management"] is False
        for op in ("model_catalog", "download_model", "install_runtime"):
            with pytest.raises(RuntimeError, match="disabled"):
                connection.request(op)
        assert connection.request("describe")["available"]
        managed.service.set_model_management(True)
        assert connection.request("model_catalog")["models"]
        managed.service.set_model_management(False)
        for op in ("model_catalog", "download_model", "install_runtime"):
            with pytest.raises(RuntimeError, match="disabled"):
                connection.request(op, family="local_whisper", model="tiny")
        assert settings_manager.load_all_settings()[SettingsKey.REMOTE_HOST_MODEL_MANAGEMENT] is False
    finally:
        connection.close()


def test_management_does_not_relax_authentication_or_revocation(managed):
    managed.service.set_model_management(True)
    stranger = RemoteConnection("127.0.0.1", managed.host.port, "wrong-token", managed.pairing.fingerprint)
    try:
        with pytest.raises(RemoteEngineError, match="isn't paired"):
            stranger.connect()
    finally:
        stranger.close()
    connection = _connect(managed.host, managed.pairing)
    try:
        # Remove just the registry entry to exercise the per-request check,
        # without relying on remove_device's proactive socket close.
        managed.host.registry.remove(managed.pairing.device_id)
        with pytest.raises(RuntimeError):
            connection.request("download_model", family="local_whisper", model="tiny")
        assert managed.service._model_manager.job() == {}
    finally:
        connection.close()


@pytest.mark.parametrize("fields", [
    {}, {"family": [], "model": "tiny"}, {"family": "local_whisper", "model": None},
    {"family": "local_whisper", "model": "tiny", "url": "https://example.com/model"},
    {"family": "local_whisper", "model": "tiny", "settings": {"remote_host_model_management": True}},
])
def test_malformed_mutations_never_reach_downloader(managed, downloads, fields):
    managed.service.set_model_management(True)
    with pytest.raises(RuntimeError, match="Invalid"):
        managed.service.remote_model_request("download_model", **fields)
    assert downloads.calls == []


@pytest.mark.parametrize("family,model", [
    ("local_whisper", "../../private"), ("local_whisper", "owner/repository"),
    ("parakeet", "tiny"), ("local_whisper", "auto"), ("api", "whisper-1"),
])
def test_only_bundled_family_model_pairs_can_be_downloaded(managed, downloads, family, model):
    managed.service.set_model_management(True)
    with pytest.raises(RuntimeError, match="catalog"):
        managed.service.remote_model_request("download_model", family=family, model=model)
    assert downloads.calls == []
    assert not downloads.coordinator.requests_in_flight


def test_catalog_is_safe_and_reports_missing_runtime_separately(managed, downloads):
    downloads.cached.update(("tiny", "parakeet-v3"))
    managed.service.set_model_management(True)
    result = managed.service.remote_model_request("model_catalog")
    entries = {entry["model"]: entry for entry in result["models"]}
    assert entries["tiny"]["cached"] and entries["tiny"]["runtime_ready"]
    assert entries["parakeet-v3"]["cached"] and not entries["parakeet-v3"]["runtime_ready"]
    assert not entries["base"]["cached"]
    assert entries["tiny"]["download_size"]
    assert set(entries["tiny"]) == {"family", "model", "label", "cached", "runtime_ready", "download_size", "dependencies", "selected_device"}
    assert all(not ({"url", "path", "archives"} & set(dep)) for dep in entries["tiny"]["dependencies"])
    assert result["can_select"]
    assert result["download"] == {}
    assert downloads.calls == []


def test_download_survives_disconnect_and_is_observable_without_selecting(managed, downloads):
    managed.service.set_model_management(True)
    downloads.release.clear()
    request = managed.service.remote_model_request
    try:
        result = request("download_model", family="local_whisper", model="tiny")
        assert result["download"]["state"] == "downloading"
        assert downloads.entered.wait(2)
        status = request("model_catalog")["download"]
        assert (status["done"], status["total"]) == (25, 100)
        assert managed.engine.model == "parakeet-v3"
        # The first request's connection has closed. A repeat joins its work.
        assert request("download_model", family="local_whisper", model="tiny")["download"] == status
        with pytest.raises(RuntimeError, match="Another remote"):
            request("download_model", family="local_whisper", model="base")
        managed.service.set_model_management(False)
        downloads.release.set()
        assert _wait_for(lambda: managed.service._model_manager.job().get("state") == "complete")
        managed.service.set_model_management(True)
        catalog = request("model_catalog")
        assert next(m for m in catalog["models"] if m["model"] == "tiny")["cached"]
        assert managed.engine.model == "parakeet-v3"
        assert downloads.calls == ["tiny"]
        request("select_model", family="local_whisper", model="tiny")
        assert managed.engine.model == "tiny"
    finally:
        downloads.release.set()


@pytest.mark.parametrize("policy,offline,reason", [
    (HuggingFaceAccessPolicy.ASK, False, "consent"),
    (HuggingFaceAccessPolicy.NEVER, False, "consent"),
    (HuggingFaceAccessPolicy.ALWAYS, True, "HF_HUB_OFFLINE"),
])
def test_download_respects_host_policy_and_releases_claim(managed, downloads, monkeypatch, policy, offline, reason):
    managed.service.set_model_management(True)
    downloads.coordinator.set_policy(policy)
    if offline:
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(RuntimeError, match=reason):
        managed.service.remote_model_request("download_model", family="local_whisper", model="tiny")
    assert not downloads.coordinator.requests_in_flight
    assert downloads.coordinator.get_policy() == policy
    assert downloads.calls == []


def test_cached_download_is_noop_and_local_claim_prevents_remote_work(downloads):
    manager = HostModelManager()
    downloads.cached.add("tiny")
    assert manager.download("local_whisper", "tiny", "laptop")["download"]["state"] == "complete"
    assert downloads.calls == []
    assert not downloads.coordinator.requests_in_flight
    assert downloads.coordinator.begin_request("base")
    try:
        with pytest.raises(RuntimeError, match="already"):
            manager.download("local_whisper", "base", "laptop")
    finally:
        downloads.coordinator.end_request("base")


def test_failed_download_is_sanitized_releases_claim_and_can_retry(downloads, monkeypatch):
    finished = threading.Event()
    manager = HostModelManager(finished.set)
    original = hf_access.download_model_files

    def fail(*args, **kwargs):
        raise OSError("secret-token in https://secret@example.com/private/path")

    monkeypatch.setattr(hf_access, "download_model_files", fail)
    manager.download("local_whisper", "tiny", "laptop")
    assert finished.wait(2)
    assert manager.job()["state"] == "failed"
    assert "secret" not in manager.job()["error"]
    assert not downloads.coordinator.requests_in_flight
    monkeypatch.setattr(hf_access, "download_model_files", original)
    finished.clear()
    manager.download("local_whisper", "tiny", "laptop")
    assert finished.wait(2)
    assert manager.job()["state"] == "complete"


def test_worker_start_failure_releases_claim_and_does_not_stay_busy(downloads, monkeypatch):
    manager = HostModelManager()

    def fail_start(_thread):
        raise RuntimeError("No threads available")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="threads"):
        manager.download("local_whisper", "tiny", "laptop")
    assert not downloads.coordinator.requests_in_flight
    assert manager.job()["state"] == "failed"


def test_incomplete_download_is_never_reported_as_ready(downloads, monkeypatch):
    finished = threading.Event()
    manager = HostModelManager(finished.set)
    monkeypatch.setattr(hf_access, "download_model_files", lambda *args, **kwargs: "partial")
    manager.download("local_whisper", "tiny", "laptop")
    assert finished.wait(2)
    assert manager.job()["state"] == "failed"
    assert not downloads.coordinator.requests_in_flight


def test_client_explains_disabled_or_older_host_without_mutating(managed, downloads, monkeypatch):
    with pytest.raises(RemoteEngineError, match="disabled"):
        managed.service.remote_model_request("download_model", family="local_whisper", model="tiny")
    send = managed.host._send

    def legacy_send(ws, message):
        message.pop("capabilities", None)
        send(ws, message)

    monkeypatch.setattr(managed.host, "_send", legacy_send)
    managed.service.set_model_management(True)
    with pytest.raises(RemoteEngineError, match="Update OpenWhisper"):
        managed.service.remote_model_request("download_model", family="local_whisper", model="tiny")
    assert downloads.calls == []


def _pump_until(predicate):
    from PyQt6.QtWidgets import QApplication

    return _wait_for(lambda: (QApplication.processEvents() or True) and predicate(), 5)


def test_management_dialog_runs_off_ui_thread_and_reflects_host_readiness(managed, downloads):
    from ui_qt.dialogs.remote_models import RemoteModelsDialog

    managed.service.set_model_management(True)
    downloads.cached.update(("tiny", "parakeet-v3"))
    dialog = RemoteModelsDialog(managed.service, "devbox")
    dialog.show()
    assert _pump_until(lambda: not dialog._busy)
    assert dialog._valid
    dialog.select_model("parakeet:parakeet-v3")
    assert not dialog.select_button.isEnabled()  # downloaded, runtime absent
    assert "runtime" in dialog.runtime_detail.text()
    dialog.select_model("local_whisper:tiny")
    assert dialog.select_button.isEnabled()
    assert not dialog.download_button.isEnabled()
    dialog._request("model_catalog")
    assert not dialog.select_button.isEnabled()  # request in flight
    assert _pump_until(lambda: not dialog._busy)
    assert dialog._choice()["model"] == "tiny"
    managed.service.set_model_management(False)
    dialog._request("model_catalog")
    assert _pump_until(lambda: not dialog._busy)
    assert "disabled" in dialog.status.text()
    assert not dialog.select_button.isEnabled()
    assert not dialog.download_button.isEnabled()
    dialog.close()
    assert not dialog._poll.isActive()


def test_dialog_confirms_download_and_polls_until_complete(managed, downloads, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from ui_qt.dialogs.remote_models import RemoteModelsDialog

    managed.service.set_model_management(True)
    downloads.release.clear()
    dialog = RemoteModelsDialog(managed.service, "devbox")
    dialog.show()
    try:
        assert _pump_until(lambda: not dialog._busy)
        dialog.select_model("local_whisper:tiny")
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
        dialog.download_button.click()
        assert downloads.calls == []
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
        dialog.download_button.click()
        assert _pump_until(lambda: dialog._poll.isActive())
        assert "25%" in dialog.status.text()
        assert not dialog.download_button.isEnabled()
        downloads.release.set()
        assert _wait_for(lambda: managed.service._model_manager.job().get("state") == "complete")
        dialog._request("model_catalog")
        assert _pump_until(lambda: not dialog._busy)
        assert dialog.select_button.isEnabled()
        assert not dialog._poll.isActive()
        assert managed.engine.model == "parakeet-v3"
    finally:
        downloads.release.set()
        dialog.close()


def test_a_dialog_closed_mid_request_is_not_held_by_the_worker():
    # The worker used to capture the dialog: it kept a closed dialog alive,
    # then dropped the last reference (deleting a QWidget) or emitted on it
    # from its own thread, which can crash the process.
    import gc
    import weakref

    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication

    from ui_qt.dialogs.remote_models import RemoteModelsDialog

    gate = threading.Event()

    def request(op, expected_pairing=None, **fields):
        gate.wait(5)
        return {"models": []}

    service = SimpleNamespace(client_pairing=lambda: None, remote_model_request=request)
    dialog = RemoteModelsDialog(service, "devbox")
    dialog.show()
    assert dialog._busy
    workers = [t for t in threading.enumerate() if t.name == "remote-model-request"]
    ref = weakref.ref(dialog)
    dialog.deleteLater()
    del dialog
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    # PyQt frees the dialog's own lambda connections with a queued call and
    # then a deferred delete; flush both.
    QApplication.processEvents()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    gc.collect()
    assert ref() is None
    gate.set()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    QApplication.processEvents()


def test_settings_tile_is_off_by_default_and_only_changes_host_permission(managed):
    from ui_qt.dialogs.settings_dialog import SettingsDialog

    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    try:
        section = dialog.remote_section
        section.bind(managed.service)
        assert not section.management_tile.checkbox.isChecked()
        section.management_tile.checkbox.setChecked(True)
        assert remote_settings.host_model_management()
        assert not remote_settings.host_enabled()  # permission alone never starts sharing
        assert section.manage_button.text() == "Manage host models"
        section.management_tile.checkbox.setChecked(False)
        assert not remote_settings.host_model_management()
    finally:
        dialog.close()
