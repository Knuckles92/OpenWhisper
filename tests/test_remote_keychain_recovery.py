"""Retry a denied credential save without repeating host authorization."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from keyring.errors import PasswordSetError
from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import QApplication

from services import credentials
from services.remote_asr import client, settings as remote_settings, tailscale
from services.remote_asr.service import NearbyScan, RemoteEngineService
from ui_qt.dialogs import settings_remote
from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE
from ui_qt.dialogs.settings_dialog import SettingsDialog

SECRET = "test-remote-connection-secret"


class DeniedBackend(credentials.MemoryCredentialBackend):
    read_allowed = False
    write_allowed = False

    def __init__(self):
        super().__init__()
        self.read_threads = []

    def get_password(self, service, username):
        if username == remote_settings.token_credential():
            self.read_threads.append(threading.get_ident())
        if not self.read_allowed:
            raise RuntimeError("access denied")
        return super().get_password(service, username)

    def set_password(self, service, username, password):
        if not self.write_allowed:
            raise PasswordSetError("access denied")
        super().set_password(service, username, password)


@pytest.fixture
def denied_pairing(monkeypatch, request):
    backend = DeniedBackend()
    credentials.set_store(credentials.CredentialStore(lambda: (backend, "macOS Keychain")))
    result = client.PairingResult(
        SECRET, "test-device", "Test host", "AB" * 32, via=getattr(request, "param", "code")
    )
    requests = []

    def pair(*args, **kwargs):
        requests.append((args, kwargs))
        return result

    monkeypatch.setattr(client, "pair_with_host", pair)
    monkeypatch.setattr(client, "request_pairing", pair)
    service = RemoteEngineService(lambda: None, bind="127.0.0.1")
    monkeypatch.setattr(service, "scan_nearby", lambda **kwargs: NearbyScan(tailscale.TailscaleStatus("not_installed")))
    yield backend, service, requests
    service.shutdown()


def pump_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


@pytest.mark.parametrize("denied_pairing", ["code", "tailscale", "approval"], indirect=True)
def test_retry_saves_the_approved_token_without_contacting_the_host_again(denied_pairing, caplog, request):
    backend, service, requests = denied_pairing
    changed = []
    service.on_client_changed = lambda: changed.append(True)
    route = request.node.callspec.params["denied_pairing"]
    with pytest.raises(credentials.CredentialStoreError):
        if route == "approval":
            service.request_pairing("192.0.2.10", on_code=lambda code: None)
        else:
            service.pair("192.0.2.10", "123456" if route == "code" else None,
                         tailscale=route == "tailscale")
    assert service.client_pairing() is None
    assert service.pending_pairing_name() == "Test host"
    assert changed == []
    with pytest.raises(credentials.CredentialStoreError):
        service.retry_pairing_save()
    backend.read_allowed = backend.write_allowed = True
    pairing = service.retry_pairing_save()
    assert pairing.host_name == "Test host"
    assert pairing.via == route
    assert remote_settings.load_client_token() == SECRET
    assert len(requests) == 1
    assert service.pending_pairing_name() == ""
    assert changed == [True]
    from services.settings import settings_manager

    assert SECRET not in str(settings_manager.load_all_settings())
    assert SECRET not in caplog.text


def test_forgetting_a_host_discards_an_unsaved_pairing(denied_pairing):
    backend, service, _ = denied_pairing
    with pytest.raises(credentials.CredentialStoreError):
        service.pair("192.0.2.10", "123456")
    service.forget_host()
    backend.read_allowed = backend.write_allowed = True
    with pytest.raises(ValueError, match="Connect to a computer"):
        service.retry_pairing_save()
    assert service.pending_pairing_name() == ""


@pytest.mark.parametrize("ui_mode", ["classic", "omarchy"])
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_page_recovers_after_repeated_denials(denied_pairing, monkeypatch, ui_mode, platform):
    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    monkeypatch.setattr(settings_remote, "sys", SimpleNamespace(platform=platform))
    backend, service, requests = denied_pairing
    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    section = dialog.remote_section
    section.bind(service)
    dialog.show()
    dialog.select_destination(REMOTE_ENGINE)
    assert section.credential_recovery.isHidden()
    section.address_edit.setText("192.0.2.10")
    section.code_edit.setText("123456")
    section.pair_button.click()
    pump_until(lambda: not section._pairing_busy)
    assert not section.credential_recovery.isHidden()
    assert "Retry access below" in section.find_message.text()
    assert "Test host" in section.credential_help.text()
    assert section.open_keychain_button.isHidden() == (platform != "darwin")
    if platform == "darwin":
        assert section.retry_key_button.text() == "Retry Keychain access"
        assert "Allow or Always Allow" in section.credential_help.text()
    else:
        assert section.retry_key_button.text() == "Retry saving key"

    section.retry_key_button.click()
    assert not section.retry_key_button.isEnabled()
    pump_until(lambda: not section._pairing_busy)
    assert not section.credential_recovery.isHidden()
    assert section.retry_key_button.isEnabled()
    assert service.client_pairing() is None

    backend.read_allowed = True
    section.retry_key_button.click()
    pump_until(lambda: not section._pairing_busy)
    assert not section.credential_recovery.isHidden()
    assert service.client_pairing() is None

    backend.write_allowed = True
    section.retry_key_button.click()
    pump_until(lambda: not section._pairing_busy)
    assert section.credential_recovery.isHidden()
    assert section.find_tile.isHidden()
    assert not section.client_tile.isHidden()
    assert service.client_pairing().host_name == "Test host"
    assert len(requests) == 1
    assert backend.read_threads
    assert threading.get_ident() not in backend.read_threads
    dialog.close()


def test_reopening_settings_keeps_the_recovery_action(denied_pairing):
    _, service, _ = denied_pairing
    with pytest.raises(credentials.CredentialStoreError):
        service.pair("192.0.2.10", "123456")
    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    dialog.remote_section.bind(service)
    dialog.select_destination(REMOTE_ENGINE)
    assert not dialog.remote_section.credential_recovery.isHidden()
    dialog.close()


def test_open_keychain_access_uses_the_system_app(denied_pairing, monkeypatch):
    opened = []
    monkeypatch.setattr(QProcess, "startDetached", lambda *args: (opened.append(args) or True, 123))
    section = settings_remote.RemoteEngineSection()
    section._open_keychain()
    assert opened == [("/usr/bin/open", ["-b", "com.apple.keychainaccess"])]
