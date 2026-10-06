"""Renaming paired computers: the host's name for a client, and a client's for its host.

Hosts and clients talk over real TLS sockets on 127.0.0.1; only the speech
engine is fake. Each side's name for the other is its own: nothing is sent.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit

from services.remote_asr import protocol, tailscale
from services.remote_asr import settings as remote_settings
from services.remote_asr.client import PairingResult
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.settings import ClientPairing
from services.remote_asr.tls import ensure_host_identity
from services.settings import SettingsKey, settings_manager
from tests.test_remote_engine import (
    FakeEngine,
    ListStore,
    _connect,
    _pair,
    _tone,
    _wait_for,
)


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    """Keep every test off this computer's real tailnet."""
    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: tailscale.TailscaleStatus("not_installed"))
    monkeypatch.setattr(tailscale, "whois", lambda address, timeout=4.0: None)


@pytest.fixture
def store():
    return ListStore()


@pytest.fixture
def host(store, tmp_path):
    engine = FakeEngine()
    speech_host = SpeechHost(
        engine_provider=lambda: engine,
        registry=DeviceRegistry(store.load, store.save),
        identity=ensure_host_identity(str(tmp_path / "host")),
        host_name="devbox",
    )
    speech_host.start(port=0, bind="127.0.0.1")
    yield speech_host
    speech_host.stop()


def _service(tmp_path):
    from services.remote_asr.service import RemoteEngineService

    return RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"),
                               records_root=str(tmp_path / "records"), bind="127.0.0.1")


def _saved_pairing():
    return remote_settings.save_client_pairing("192.168.1.20", 47821, PairingResult(
        token="t", device_id="d", host_name="DESKTOP-4K2J9", fingerprint="AB" * 32, via="approval",
    ))


# ---- the host's name for a paired computer ----

def test_a_paired_computer_can_go_by_another_name_and_back(store):
    registry = DeviceRegistry(store.load, store.save)
    device, token = registry.add("DESKTOP-LAPTOP")
    renamed = registry.rename(device["id"], "  Kitchen laptop\n")
    assert renamed["name"] == "Kitchen laptop" and renamed["paired_name"] == "DESKTOP-LAPTOP"
    assert "token_sha256" not in renamed and registry.list() == [renamed]
    # Renamed again, it still knows the name it paired with.
    assert registry.rename(device["id"], "Studio")["paired_name"] == "DESKTOP-LAPTOP"
    back = registry.rename(device["id"], "")
    assert back["name"] == "DESKTOP-LAPTOP" and "paired_name" not in back
    registry.rename(device["id"], "Studio")
    assert "paired_name" not in registry.rename(device["id"], "DESKTOP-LAPTOP")
    assert registry.rename(device["id"], "x" * 200)["name"] == "x" * protocol.MAX_NAME
    assert registry.rename("not-paired", "Studio") is None
    assert registry.authenticate(token)["id"] == device["id"]


def test_a_connected_computer_goes_by_its_new_name_at_once(host):
    events = []
    host._on_event = lambda kind, _detail: events.append(kind)
    result = _pair(host)
    connection = _connect(host, result)
    try:
        connection.request("transcribe", audio=_tone(1600))
        assert host.rename_device(result.device_id, "Kitchen laptop")["name"] == "Kitchen laptop"
        assert [client["name"] for client in host.connected_clients()] == ["Kitchen laptop"]
        assert host.activity.snapshot()["devices"][result.device_id]["name"] == "Kitchen laptop"
        assert {"devices", "clients"} <= set(events)
        # The connection opened before the rename makes its next request under the new name.
        connection.request("transcribe", audio=_tone(1600))
        newest = host.activity.snapshot(events=1)["events"][0]
        assert newest["kind"] == "transcribed" and newest["name"] == "Kitchen laptop"
    finally:
        connection.close()
    assert _wait_for(lambda: host.activity.snapshot(events=1)["events"][0]["kind"] == "disconnected")
    assert host.activity.snapshot(events=1)["events"][0]["name"] == "Kitchen laptop"


def test_the_service_renames_a_paired_computer_on_what_it_keeps_too(tmp_path, monkeypatch):
    from services.remote_records.host_store import HostRecordStore

    service = _service(tmp_path)
    events = []
    service.add_listener(events.append)
    device, _ = service.history.registry.add("DESKTOP-LAPTOP")
    renamed = []
    monkeypatch.setattr(HostRecordStore, "rename_device",
                        lambda self, owner, name: renamed.append((owner, name)) or 1)
    try:
        assert service.rename_device(device["id"], "Kitchen laptop")["name"] == "Kitchen laptop"
        assert service.history.registry.list()[0]["paired_name"] == "DESKTOP-LAPTOP"
        assert renamed == [(device["id"], "Kitchen laptop")]
        assert events == ["devices", "records"]
        assert service.rename_device("not-paired", "Studio") is None
    finally:
        service.shutdown()


# ---- this computer's name for its host ----

def test_the_host_can_go_by_another_name_here_and_back():
    pairing = _saved_pairing()
    renamed = remote_settings.rename_client_host(" Studio PC ")
    assert renamed.host_name == "Studio PC" and renamed.paired_name == "DESKTOP-4K2J9"
    loaded = remote_settings.load_client_pairing()
    assert loaded.host_name == "Studio PC" and loaded.renamed
    # Still the pairing a window opened before the rename holds.
    assert loaded == pairing
    moved = remote_settings.remember_host_addresses(
        "AB" * 32, "192.168.1.37", 47821, ["192.168.1.37"])
    assert moved.host_name == "Studio PC" and moved.paired_name == "DESKTOP-4K2J9"
    back = remote_settings.rename_client_host("")
    assert back.host_name == "DESKTOP-4K2J9" and not back.renamed
    saved = settings_manager.load_all_settings()[SettingsKey.REMOTE_ENGINE_CLIENT]
    assert saved["host_name"] == "DESKTOP-4K2J9" and "paired_name" not in saved


def test_renaming_the_host_with_nothing_paired_does_nothing():
    assert remote_settings.rename_client_host("Studio PC") is None
    assert remote_settings.load_client_pairing() is None


def test_only_a_host_that_isnt_renamed_goes_by_the_name_it_reports():
    pairing = ClientPairing("10.0.0.2", 47821, "AB" * 32, "DESKTOP-4K2J9")
    assert pairing.name_for("NEW-NAME") == "NEW-NAME"
    assert pairing.name_for("") == "DESKTOP-4K2J9"
    renamed = replace(pairing, host_name="Studio PC", paired_name="DESKTOP-4K2J9")
    assert renamed.name_for("NEW-NAME") == "Studio PC"


def test_the_service_renames_its_host_and_says_so(tmp_path):
    service = _service(tmp_path)
    events = []
    service.add_listener(events.append)
    try:
        assert service.rename_host("Studio PC") is None and events == []
        _saved_pairing()
        assert service.rename_host("Studio PC").host_name == "Studio PC"
        assert events == ["host_renamed"]
        assert service.client_pairing().host_name == "Studio PC"
    finally:
        service.shutdown()


def test_dictation_calls_the_host_its_new_name_without_reconnecting(host):
    from transcriber.remote_backend import RemoteSpeechBackend

    remote_settings.save_client_pairing("127.0.0.1", host.port, _pair(host))
    backend = RemoteSpeechBackend()
    try:
        backend.reload_model()
        assert backend.name == "Fake Parakeet on devbox"
        connection = backend._process
        backend.host_renamed(remote_settings.rename_client_host("Studio PC"))
        assert backend.name == "Fake Parakeet on Studio PC" and backend._process is connection
        assert backend.link().host == "Studio PC"
        # A fresh connection keeps the name, though the host still calls itself devbox.
        backend.reload_model()
        assert backend.is_available() and backend.name == "Fake Parakeet on Studio PC"
    finally:
        backend.cleanup()


# ---- Settings ----

def _device_rows(section, kind, name):
    """The rows' widgets once the rows a refresh replaced are gone."""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return section.devices_list.findChildren(kind, name)


def test_the_name_dialog_starts_from_the_current_name(monkeypatch):
    from ui_qt.dialogs.settings_remote import _ask_name

    seen = []

    def type_name(dialog):
        edit = dialog.findChild(QLineEdit, "remoteRenameEdit")
        seen.append((edit.text(), edit.placeholderText(), edit.maxLength()))
        edit.setText("Studio PC")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", type_name)
    assert _ask_name(None, "Rename host", "", "Old name", "DESKTOP-4K2J9") == "Studio PC"
    assert seen == [("Old name", "DESKTOP-4K2J9", protocol.MAX_NAME)]
    monkeypatch.setattr(QDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    assert _ask_name(None, "Rename host", "", "Old name", "DESKTOP-4K2J9") is None


def test_settings_renames_a_paired_computer(tmp_path, monkeypatch):
    from tests.test_remote_records_ui import FakeRecords, _section
    from ui_qt.dialogs import settings_remote
    from ui_qt.widgets import Button, WrappedLabel

    service = _service(tmp_path)
    service.history.registry.add("DESKTOP-LAPTOP")
    asked = []

    def answer(_parent, title, _text, current, own):
        asked.append((title, current, own))
        return "Kitchen laptop"

    monkeypatch.setattr(settings_remote, "_ask_name", answer)
    dialog, section = _section(FakeRecords(), service)
    try:
        [rename] = _device_rows(section, Button, "remoteRenameDeviceButton")
        rename.click()
        assert asked == [("Rename paired computer", "DESKTOP-LAPTOP", "DESKTOP-LAPTOP")]
        assert service.history.registry.list()[0]["name"] == "Kitchen laptop"
        [label] = _device_rows(section, WrappedLabel, "remoteDeviceLabel")
        assert label.text().startswith("Kitchen laptop · paired ")
        assert " as DESKTOP-LAPTOP" in label.text()
    finally:
        service.shutdown()
        dialog.close()


def test_settings_renames_the_paired_host(tmp_path, monkeypatch):
    from tests.test_remote_records_ui import FakeRecords, _section
    from ui_qt.dialogs import settings_remote

    _saved_pairing()
    service = _service(tmp_path)
    asked = []
    monkeypatch.setattr(settings_remote, "_ask_name",
                        lambda _parent, title, _text, current, own: asked.append((current, own)) or "Studio PC")
    dialog, section = _section(FakeRecords(), service)
    try:
        assert section.rename_host_button.isVisibleTo(dialog)
        section.rename_host_button.click()
        QApplication.processEvents()
        assert asked == [("DESKTOP-4K2J9", "DESKTOP-4K2J9")]
        assert service.client_pairing().host_name == "Studio PC"
        assert section.client_message.text() == "This computer now calls the host Studio PC."
        assert section.client_tile.description_label.text().startswith(
            "Paired with Studio PC (DESKTOP-4K2J9) at 192.168.1.20")
    finally:
        service.shutdown()
        dialog.close()


def test_the_mcp_page_names_the_host_by_its_new_name(tmp_path):
    from tests.test_mcp_host_tabs import FakeRemoteService, close, make_view, pump

    pairing = ClientPairing("10.0.0.2", 47821, "AB" * 32, "jed")
    service = FakeRemoteService(pairing=pairing)
    view = make_view(tmp_path, service)
    try:
        view.show()
        pump(view)
        assert [button.text() for button in view.tabs.buttons] == ["This computer", "jed"]
        service.pairing = replace(pairing, host_name="Studio PC", paired_name="jed")
        view._on_service_event("host_renamed")
        pump(view)
        assert [button.text() for button in view.tabs.buttons] == ["This computer", "Studio PC"]
    finally:
        close(view)
