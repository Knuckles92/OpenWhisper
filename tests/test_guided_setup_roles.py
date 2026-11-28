"""Tests for role-aware Guided Setup, Remote Settings host services/invites, and Client-Only UI."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QApplication

from services import guided_setup
from services.remote_asr import invite as remote_invite
from services.remote_asr.settings import ClientPairing
from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs import guided_setup as setup_ui
from ui_qt.dialogs import (
    settings_dialog,
    settings_downloads,
    settings_models,
    settings_remote,
)
from ui_qt.dialogs.settings_destinations import (
    DOWNLOADS,
    REMOTE_ENGINE,
    VOICE_MODEL,
)

FP = "C3D4E5F6" * 8


class _FakeRemoteService:
    """In-memory RemoteEngineService stand-in exposing Phase 1 & Phase 4 hooks."""

    def __init__(self, *, pairing=None, running=True):
        self._pairing = pairing
        self._running = running
        self._listeners = []
        self._host_caps = {
            "text_cleanup": False,
            "fast_judgments": False,
            "walkthrough_writeup": False,
            "meeting_ai": False,
            "agent_ask": False,
            "keep_records": False,
            "model_management": False,
            "manage_mcp": False,
            "invite_enabled": False,
            "invite_cache": False,
        }
        self.registered_invite_server = None
        self.host_enabled = running

    def add_listener(self, fn):
        self._listeners.append(fn)

    def remove_listener(self, fn):
        if fn in self._listeners:
            self._listeners.remove(fn)

    def client_pairing(self):
        return self._pairing

    def tailscale_status(self, *, refresh=False):
        return SimpleNamespace(running=False, state="not_installed", owner="", address="", name="", peers=())

    def scan_nearby(self, *, sweep=False):
        return SimpleNamespace(status=self.tailscale_status(), hosts=(), swept=sweep)

    def open_pairing(self):
        return "246810"

    def set_host_enabled(self, enabled):
        self.host_enabled = bool(enabled)
        self._running = bool(enabled)

    def set_text_cleanup(self, enabled):
        self._host_caps["text_cleanup"] = bool(enabled)

    def set_fast_judgments(self, enabled):
        self._host_caps["fast_judgments"] = bool(enabled)

    def set_walkthrough_writeup(self, enabled):
        self._host_caps["walkthrough_writeup"] = bool(enabled)

    def set_meeting_ai(self, enabled):
        self._host_caps["meeting_ai"] = bool(enabled)

    def set_agent_ask(self, enabled):
        self._host_caps["agent_ask"] = bool(enabled)

    def set_keep_records(self, enabled):
        self._host_caps["keep_records"] = bool(enabled)

    def set_model_management(self, enabled):
        self._host_caps["model_management"] = bool(enabled)

    def set_manage_mcp(self, enabled):
        self._host_caps["manage_mcp"] = bool(enabled)

    def set_invite_enabled(self, enabled):
        self._host_caps["invite_enabled"] = bool(enabled)

    def set_invite_cache(self, enabled):
        self._host_caps["invite_cache"] = bool(enabled)

    def register_invite_server(self, server):
        self.registered_invite_server = server

    def host_state(self):
        return {
            "enabled": self.host_enabled,
            "running": self._running,
            "port": 47821,
            "error": "",
            "host_name": "Studio-GPU",
            "address": "127.0.0.1",
            "address_kind": "lan",
            "fingerprint": FP,
            "pairing": None,
            "clients": [],
            "devices": [],
            "engine": {"label": "Parakeet V3", "device": "cuda", "available": True},
            "tailscale": self.tailscale_status(),
            "tailscale_trust": True,
            "pair_request": None,
            "notes": [],
            **self._host_caps,
        }


@pytest.fixture
def isolated_ui(tmp_path, monkeypatch):
    store = SettingsManager(str(tmp_path / "settings.json"))
    for mod in (settings_dialog, settings_models, settings_downloads, settings_remote, setup_ui, guided_setup):
        monkeypatch.setattr(mod, "settings_manager", store)
    monkeypatch.setattr(settings_dialog.history_manager, "set_retention", MagicMock())
    for mod in (settings_models, settings_downloads):
        monkeypatch.setattr(mod, "scan_cached_models", lambda *a, **k: {})
    monkeypatch.setattr("services.local_asr.cache.inventory", lambda *a, **k: {})
    monkeypatch.setattr(settings_dialog.AudioRecorder, "get_input_devices", lambda: [])
    monkeypatch.setattr(guided_setup, "speech_model_downloaded", lambda _s: True)
    monkeypatch.delenv("OPENWHISPER_EDITION", raising=False)
    return store


def test_full_guided_setup_offers_three_roles_and_routes_client_and_host_paths(isolated_ui, monkeypatch):
    store = isolated_ui
    fake_service = _FakeRemoteService(
        pairing=ClientPairing(host="192.168.1.30", port=47821, fingerprint=FP, host_name="Studio-GPU")
    )
    monkeypatch.setattr(
        remote_invite,
        "fetch_host_capabilities",
        lambda _svc: {
            "capabilities": {
                "speech": True,
                "text_cleanup": True,
                "fast_judgments": True,
                "walkthrough_writeup": True,
                "meeting_ai": True,
                "records": True,
            },
            "ai": {},
            "engine": {},
            "host_name": "Studio-GPU",
        },
    )

    built_settings = []

    def get_settings():
        dlg = settings_dialog.SettingsDialog(background_cache_scan=False)
        dlg.remote_section.bind(fake_service)
        built_settings.append(dlg)
        return dlg

    setup = setup_ui.GuidedSetupDialog(
        get_settings,
        open_destination=MagicMock(),
        open_all_settings=MagicMock(),
        download_model=MagicMock(),
        manager=store,
        remote_service=fake_service,
    )
    setup.begin(welcome=True)

    # Full build Welcome shows all 3 role cards without eagerly constructing Settings.
    assert setup.page == setup_ui.WELCOME
    assert built_settings == []
    assert set(setup.role_cards.keys()) == {setup_ui.ROLE_LOCAL, setup_ui.ROLE_CLIENT, setup_ui.ROLE_HOST}

    # Path 2: Connect to an OpenWhisper Host -> CONNECT_HOST -> HOST_AI -> RECORD -> OUTPUT -> READY.
    setup.select_role(setup_ui.ROLE_CLIENT)
    setup.next_button.click()
    assert setup.page == setup_ui.CONNECT_HOST

    # Paste invite link displays parsed fingerprint preview.
    link = remote_invite.InviteLink("192.168.1.30", 47821, FP, "123456", "Studio-GPU")
    setup.invite_input.setText(link.uri)
    assert link.short_fingerprint in setup.invite_preview.text()

    setup.next_button.click()
    assert setup.page == setup_ui.HOST_AI
    setup._on_caps_done({
        "capabilities": {
            "text_cleanup": True,
            "fast_judgments": True,
            "walkthrough_writeup": True,
            "meeting_ai": True,
            "speaker_diarization": False,
            "records": True,
        }
    })
    setup.use_host_ai_switch.setChecked(True)
    setup.use_host_records_switch.setChecked(True)
    setup.apply_host_ai_button.click()
    assert store.get(SettingsKey.SELECTED_MODEL) == "remote"
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER) == "remote_host"
    assert store.get(SettingsKey.TYPESAFE_ENABLED) is True
    assert store.get(SettingsKey.TYPESAFE_PROVIDER) == "remote_host"
    assert store.get(SettingsKey.WALKTHROUGH_WRITE_UP_AGENT) == "remote_host"
    assert store.get(SettingsKey.MEETING_AGENT_CORE) == "remote_host"
    # Speaker diarization is gated separately from meeting_ai!
    assert store.get(SettingsKey.MEETING_SPEAKER_ID_BACKEND) != "remote_host"
    assert store.get(SettingsKey.REMOTE_RECORDS_LOCATION) == "host"

    # When speaker_diarization is advertised, apply_host_ai_defaults enables it too.
    remote_invite.apply_host_ai_defaults(
        {"meeting_ai": True, "speaker_diarization": True}, manager=store
    )
    assert store.get(SettingsKey.MEETING_SPEAKER_ID_BACKEND) == "remote_host"

    # Next goes straight to Record (skipping local CLEAN_UP and hiding local download button!).
    setup.next_button.click()
    assert setup.page == setup_ui.RECORD
    assert setup.speech_download_button.isHidden()

    # Path 3: Set up as a Host -> toggles capabilities + generates invite link & QR preview!
    setup.begin(welcome=True)
    setup.select_role(setup_ui.ROLE_HOST)
    setup.next_button.click()
    assert setup.page == setup_ui.HOST_SETUP

    setup.host_cap_switches["text_cleanup"].setChecked(True)
    setup.host_cap_switches["meeting_ai"].setChecked(True)
    setup.host_cap_switches["records"].setChecked(True)
    assert fake_service._host_caps["text_cleanup"] is True
    assert fake_service._host_caps["meeting_ai"] is True
    assert fake_service._host_caps["keep_records"] is True

    # Use ephemeral port for test invite server.
    remote_invite.save_setting_compat(*remote_invite.HOST_INVITE_PORT_SPEC, 0, store)
    setup.host_copy_invite_button.click()
    try:
        assert "/join/" in setup._host_invite_url
        assert not setup.host_qr_frame.isHidden()
        assert setup.host_qr_label.pixmap() is not None and not setup.host_qr_label.pixmap().isNull()
        setup.host_revoke_invite_button.click()
        assert setup._host_invite_url == ""
        assert setup.host_qr_frame.isHidden()
    finally:
        if setup._host_invite_server is not None:
            setup._host_invite_server.stop()


def test_client_only_guided_setup_starts_on_connect_host(isolated_ui, monkeypatch):
    store = isolated_ui
    monkeypatch.setenv("OPENWHISPER_EDITION", "client")
    fake_service = _FakeRemoteService(pairing=None, running=False)

    setup = setup_ui.GuidedSetupDialog(
        lambda: settings_dialog.SettingsDialog(background_cache_scan=False),
        open_destination=MagicMock(),
        open_all_settings=MagicMock(),
        download_model=MagicMock(),
        manager=store,
        remote_service=fake_service,
    )
    setup.begin(welcome=True)
    assert setup.page == setup_ui.CONNECT_HOST
    assert [t for _, t in setup._active_steps()] == ["Connect", "Host AI", "Record", "Output"]

    # Deep link jumps cleanly with invite pre-filled.
    link = remote_invite.InviteLink("192.168.1.99", 47821, FP, "654321", "MainPC")
    setup.begin_with_invite(link)
    assert setup.page == setup_ui.CONNECT_HOST
    assert setup.invite_input.text() == link.uri


def test_remote_settings_host_ai_toggles_invite_card_and_client_capabilities(isolated_ui, monkeypatch):
    store = isolated_ui
    pairing = ClientPairing(host="192.168.1.30", port=47821, fingerprint=FP, host_name="Studio-GPU")
    fake_service = _FakeRemoteService(pairing=pairing, running=True)
    remote_invite.save_setting_compat(*remote_invite.HOST_INVITE_PORT_SPEC, 0, store)

    dlg = settings_dialog.SettingsDialog(background_cache_scan=False)
    dlg.select_destination(REMOTE_ENGINE)
    dlg.remote_section.bind(fake_service)
    remote_sec = dlg.remote_section

    # Host tab: toggle AI services & LAN installer cache.
    remote_sec.tabs.setCurrentIndex(1)
    remote_sec.text_cleanup_tile.checkbox.setChecked(True)
    remote_sec.fast_judgments_tile.checkbox.setChecked(True)
    remote_sec.walkthrough_writeup_tile.checkbox.setChecked(True)
    remote_sec.meeting_ai_tile.checkbox.setChecked(True)
    remote_sec.agent_ask_tile.checkbox.setChecked(True)
    remote_sec.invite_cache_tile.checkbox.setChecked(True)
    assert fake_service._host_caps["text_cleanup"] is True
    assert fake_service._host_caps["fast_judgments"] is True
    assert fake_service._host_caps["walkthrough_writeup"] is True
    assert fake_service._host_caps["meeting_ai"] is True
    assert fake_service._host_caps["agent_ask"] is True
    assert fake_service._host_caps["invite_cache"] is True

    # Generate & revoke invite link on Host tab.
    remote_sec.invite_generate_button.click()
    try:
        assert "/join/" in remote_sec._active_invite_url
        assert remote_sec.invite_copy_button.isEnabled()
        assert remote_sec.invite_qr_button.isEnabled()
        assert remote_sec.invite_revoke_button.isEnabled()
        remote_sec.invite_revoke_button.click()
        assert remote_sec._active_invite_url == ""
        assert not remote_sec.invite_revoke_button.isEnabled()
    finally:
        if remote_sec._invite_server is not None:
            remote_sec._invite_server.stop()

    # Client tab: host capabilities card & quick "Use host" switches.
    remote_sec.tabs.setCurrentIndex(0)
    remote_sec._on_capabilities_loaded(
        remote_sec._token,
        {
            "fingerprint": FP,
            "capabilities": {
                "text_cleanup": True,
                "fast_judgments": True,
                "walkthrough_writeup": False,
                "meeting_ai": True,
                "speaker_diarization": False,
                "records": True,
            },
            "ai": {"text_cleanup": {"enabled": True, "available": True, "label": "OpenAI gpt-4o-mini"}},
        },
    )
    assert not remote_sec.capabilities_tile.isHidden()
    assert remote_sec.client_use_host_switches["text_cleanup"].isEnabled()
    assert not remote_sec.client_use_host_switches["walkthrough_writeup"].isEnabled()
    remote_sec.client_use_host_switches["text_cleanup"].setChecked(True)
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER) == "remote_host"
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED) is True
    remote_sec.client_use_host_switches["fast_judgments"].setChecked(True)
    assert store.get(SettingsKey.TYPESAFE_ENABLED) is True
    assert store.get(SettingsKey.TYPESAFE_PROVIDER) == "remote_host"
    remote_sec.client_use_host_switches["fast_judgments"].setChecked(False)
    assert store.get(SettingsKey.TYPESAFE_ENABLED) is False
    remote_sec.client_use_host_switches["meeting_ai"].setChecked(True)
    assert store.get(SettingsKey.MEETING_AGENT_CORE) == "remote_host"
    assert store.get(SettingsKey.MEETING_SPEAKER_ID_BACKEND) != "remote_host"


def test_client_only_settings_hides_host_tab_and_local_model_panels(isolated_ui, monkeypatch):
    monkeypatch.setenv("OPENWHISPER_EDITION", "client")
    pairing = ClientPairing(host="192.168.1.30", port=47821, fingerprint=FP, host_name="Studio-GPU")
    fake_service = _FakeRemoteService(pairing=pairing, running=False)

    dlg = settings_dialog.SettingsDialog(background_cache_scan=False)
    dlg.bind_remote_service(fake_service)

    # 1. Remote engine hides "Share this computer" tab.
    dlg.select_destination(REMOTE_ENGINE)
    dlg.remote_section.bind(fake_service)
    assert dlg.remote_section.tabs.isHidden()
    assert dlg.remote_section.host_container.isHidden()
    assert not dlg.remote_section.client_container.isHidden()

    # 2. Voice model hides local engines/downloads, shows Powered-by-Host card + API fallback.
    dlg.select_destination(VOICE_MODEL)
    engines = [
        dlg.models.engine_combo.itemData(i) for i in range(dlg.models.engine_combo.count())
    ]
    assert engines == ["remote", "api"]
    assert not dlg.models.client_host_card.isHidden()
    assert dlg.models.ondemand_whisper_field.isHidden()
    assert dlg.models.engine_inventory_row.isHidden()
    assert "Get Full OpenWhisper" in dlg.models.client_full_edition_link.text()

    # 3. Runtime page hides local Whisper device/quantization and shows Powered-by-Host card.
    from PyQt6.QtWidgets import QVBoxLayout, QWidget

    rt_holder = QWidget()
    dlg.models.build_runtime_page(QVBoxLayout(rt_holder))
    assert not dlg.models.runtime_client_card.isHidden()
    assert dlg.models.runtime_local_section.isHidden()

    # 4. Downloads page hides local pane & target bar, shows host pane + Powered-by-Host banner.
    dlg.select_destination(DOWNLOADS)
    assert dlg.downloads.target_bar.isHidden()
    assert dlg.downloads.local_pane.isHidden()
    assert not dlg.downloads.client_edition_banner.isHidden()
    assert "Powered by Studio-GPU" in dlg.downloads.client_edition_banner_label.text()
    assert dlg.hf_policy_combo.isHidden()

    # HTML-unsafe host_name is escaped in RichText banner.
    fake_service._pairing = ClientPairing(
        host="192.168.1.30", port=47821, fingerprint=FP, host_name="<b>Evil&Host</b>"
    )
    dlg.downloads._refresh_remote_pairing()
    banner_html = dlg.downloads.client_edition_banner_label.text()
    assert "&lt;b&gt;Evil&amp;Host&lt;/b&gt;" in banner_html
    assert "<b>Evil" not in banner_html

    # Unpaired Client-Only Downloads page shows clean unpaired card.
    fake_service._pairing = None
    dlg.downloads._refresh_remote_pairing()
    assert not dlg.downloads.client_unpaired_card.isHidden()
    assert dlg.downloads.local_pane.isHidden()


def test_write_up_agent_labeling_skips_not_installed_for_remote_host(isolated_ui, monkeypatch):
    from ui_qt.widgets import agent_picker

    monkeypatch.setattr(agent_picker, "cached_agents", lambda: {})
    dlg = settings_dialog.SettingsDialog(background_cache_scan=False)
    dlg.ensure_page("walkthroughs")
    combo = dlg.walkthrough_write_up_agent_combo
    combo.addItem("Paired host", "remote_host")
    monkeypatch.setattr(
        "services.walkthrough.write_up.agent_name",
        lambda a: "Paired host" if a == "remote_host" else a,
    )
    dlg._label_write_up_agents()
    idx = combo.findData("remote_host")
    assert idx >= 0
    assert combo.itemText(idx) == "Paired host"
    assert "(not installed)" not in combo.itemText(idx)
    QApplication.processEvents()
