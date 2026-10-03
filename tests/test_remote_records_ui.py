"""Qt surfaces of records kept on the paired host: Settings, History, Past Meetings.

The record sync is a fake here; tests/test_remote_records.py drives the real
one over TLS.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from services.remote_records import sync as sync_module
from services.remote_records.sync import SyncStatus


class FakeRecords:
    def __init__(self, **status):
        self.fields = dict(location="local", paired=True, host_name="devbox")
        self.fields.update(status)
        self.locations = []
        self.remote = {"dictation": [], "meeting": []}
        self.deleted = []
        self.checked_out = []
        self.woken = []

    def status(self):
        return SyncStatus(**self.fields)

    def location(self):
        return self.fields["location"]

    def set_location(self, location):
        self.locations.append(location)
        self.fields["location"] = location

    def add_listener(self, listener):
        pass

    def remove_listener(self, listener):
        pass

    def refresh_summary(self):
        pass

    def wake(self, now=False):
        self.woken.append(now)

    def record_saved(self, kind, record_id):
        pass

    def record_deleted(self, kind, record_id):
        pass

    def cleared(self, kind, keep=()):
        pass

    def listing_wanted(self, kind):
        return bool(self.remote[kind])

    def list_remote(self, kind, query="", limit=100):
        return [dict(item, stored_on="devbox") for item in self.remote[kind]]

    def copies_on_host(self, kind, ids):
        return set()

    def delete_remote(self, kind, record_id):
        self.deleted.append((kind, record_id))
        self.remote[kind] = [item for item in self.remote[kind] if item["id"] != record_id]
        return True

    def check_out(self, kind, record_id, progress=None):
        if progress is not None:
            progress(50, 100)
        self.checked_out.append((kind, record_id))


@pytest.fixture
def records(monkeypatch):
    fake = FakeRecords()
    monkeypatch.setattr(sync_module.record_sync, "_instance", fake)
    return fake


def _pump(until, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return until()


class _Service:
    """Just enough RemoteEngineService for the page."""

    def __init__(self, paired=True):
        self.paired = paired
        self.keep = []

    def add_listener(self, listener):
        pass

    def remove_listener(self, listener):
        pass

    def client_pairing(self):
        from services.remote_asr.settings import ClientPairing

        return ClientPairing("10.0.0.2", 47821, "AB" * 32, "devbox") if self.paired else None

    def tailscale_status(self, refresh=False):
        return None

    def host_state(self):
        return {"enabled": False, "running": False, "port": 47821, "error": "", "host_name": "me",
                "address": None, "address_kind": None, "fingerprint": "", "pairing": None,
                "clients": [], "devices": [], "engine": None, "tailscale": None,
                "tailscale_trust": True, "model_management": False, "keep_records": False}

    def set_keep_records(self, enabled):
        self.keep.append(enabled)


def _section(records, service):
    from PyQt6.QtWidgets import QVBoxLayout, QWidget

    from ui_qt.dialogs.settings_remote import RemoteEngineSection

    class Dialog(QWidget):
        def __init__(self):
            super().__init__()
            self.rail = type("Rail", (), {"set_value": lambda *_a: None})()

        def _tile_group(self, layout, title, tiles, columns=1, intro=""):
            for tile in tiles:
                layout.addWidget(tile)

    dialog = Dialog()
    layout = QVBoxLayout(dialog)
    section = RemoteEngineSection(dialog, records=records)
    from PyQt6.QtGui import QIcon

    section.build(dialog, layout, lambda _name: QIcon())
    section.bind(service)
    dialog.show()
    QApplication.processEvents()
    return dialog, section


def test_the_storage_choice_shows_only_while_paired():
    records = FakeRecords(paired=False)
    dialog, section = _section(records, _Service(paired=False))
    assert not section.storage_tile.isVisibleTo(dialog)
    records.fields["paired"] = True
    section.refresh()
    assert section.storage_tile.isVisibleTo(dialog)
    assert section.location_buttons["local"].isChecked()
    assert section.location_buttons["host"].text() == "devbox"
    dialog.close()


def test_choosing_a_location_saves_it_and_says_what_happens():
    records = FakeRecords()
    dialog, section = _section(records, _Service())
    section.location_buttons["both"].click()
    assert records.locations == ["both"]
    assert section.location_buttons["both"].isChecked()
    assert "copied to devbox" in section.storage_status.text()
    assert section.send_existing_button.text() == "Copy existing records"
    dialog.close()


def test_a_host_that_refuses_says_how_to_turn_it_on():
    records = FakeRecords(location="host", host_keeps=False, host_supports=True, pending=2)
    dialog, section = _section(records, _Service())
    text = section.storage_status.text()
    assert "Keep records for paired computers" in text and "2 waiting to be sent" in text
    assert not section.send_existing_button.isEnabled()
    dialog.close()


def test_what_the_host_keeps_is_counted_and_can_be_brought_back():
    records = FakeRecords(stored={"dictation": {"count": 3, "bytes": 2048},
                                  "meeting": {"count": 1, "bytes": 0}})
    dialog, section = _section(records, _Service())
    assert "On devbox: 3 dictations and 1 meeting (2.0 KB)." in section.storage_status.text()
    assert section.bring_back_button.isVisibleTo(dialog)
    dialog.close()


def test_the_host_toggle_saves_the_opt_in():
    service = _Service()
    dialog, section = _section(FakeRecords(), service)
    assert not section.keep_records_tile.checkbox.isChecked()
    section.keep_records_tile.checkbox.setChecked(True)
    assert service.keep == [True]
    dialog.close()


@pytest.mark.parametrize("mode", ["classic", "omarchy"])
def test_client_history_sharing_is_separate_from_storage(mode, monkeypatch):
    from services.settings import SettingsKey, settings_manager

    monkeypatch.setenv("OPENWHISPER_UI", mode)

    class HistoryService(_Service):
        def set_share_history(self, enabled):
            settings_manager.save_setting(SettingsKey.REMOTE_CLIENT_HISTORY, enabled)

    records = FakeRecords()
    dialog, section = _section(records, HistoryService())
    dialog.resize(520, 740)
    QApplication.processEvents()
    assert section.share_history_tile.isVisibleTo(dialog)
    assert not section.share_history_tile.checkbox.isChecked()
    assert section.share_history_tile.minimumSizeHint().width() <= 520
    section.share_history_tile.checkbox.setChecked(True)
    assert settings_manager.get(SettingsKey.REMOTE_CLIENT_HISTORY) is True
    assert records.location() == "local" and records.locations == []
    section.refresh()
    assert section.share_history_tile.checkbox.isChecked()
    section.share_history_tile.checkbox.setChecked(False)
    assert settings_manager.get(SettingsKey.REMOTE_CLIENT_HISTORY) is False
    dialog.close()


def _remote_entry(entry_id, text, when="2026-09-27T10:00:00+00:00", audio=True):
    return {"id": entry_id, "text": text, "raw_text": None, "timestamp": when,
            "model": "parakeet (cuda)", "transcription_time": 1.0, "audio_duration": 2.0,
            "file_size": 64000, "cleanup_provider": None, "cleanup_model": None,
            "source_name": None, "has_audio": audio, "audio_bytes": 64000}


def test_history_merges_the_hosts_entries_by_time(records):
    from services.history_manager import history_manager
    from ui_qt.widgets.history_sidebar import HistoryItemWidget, HistorySidebar

    local = history_manager.add_entry(text="kept here", model="parakeet")
    records.remote["dictation"] = [
        _remote_entry("remote-old", "old one", "2000-01-01T00:00:00+00:00"),
        _remote_entry("remote-new", "new one", "2999-01-01T00:00:00+00:00"),
    ]
    sidebar = HistorySidebar()
    sidebar.expand()

    def items():
        layout = sidebar.history_list_layout
        return [layout.itemAt(i).widget() for i in range(layout.count())
                if isinstance(layout.itemAt(i).widget(), HistoryItemWidget)]

    assert _pump(lambda: len(items()) == 3)
    assert [item.entry.id for item in items()] == ["remote-new", local.id, "remote-old"]
    remote = items()[0]
    assert remote.location_chip.text() == "On devbox"
    assert remote.retranscribe_btn.isVisible() or remote.retranscribe_btn.isVisibleTo(sidebar)
    assert sidebar.entry_for("remote-new").stored_on == "devbox"

    with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes):
        sidebar._on_delete_requested("remote-new")
    assert _pump(lambda: records.deleted == [("dictation", "remote-new")])
    assert _pump(lambda: len(items()) == 2)
    assert history_manager.get_entry_by_id(local.id) is not None
    sidebar.deleteLater()


def test_a_slow_host_doesnt_hold_up_this_computers_history(records, monkeypatch):
    from services.history_manager import history_manager
    from ui_qt.widgets.history_sidebar import HistoryItemWidget, HistorySidebar

    history_manager.add_entry(text="kept here", model="parakeet")
    records.remote["dictation"] = [_remote_entry("remote-new", "new one", "2999-01-01T00:00:00+00:00")]
    slow = records.list_remote

    def list_slowly(*args, **kwargs):
        time.sleep(1.0)
        return slow(*args, **kwargs)

    monkeypatch.setattr(records, "list_remote", list_slowly)
    sidebar = HistorySidebar()
    sidebar.expand()

    def count():
        layout = sidebar.history_list_layout
        return sum(isinstance(layout.itemAt(i).widget(), HistoryItemWidget) for i in range(layout.count()))

    assert _pump(lambda: count() == 1, timeout=0.9)
    assert _pump(lambda: count() == 2, timeout=3.0)
    sidebar.deleteLater()


def test_opening_a_host_meeting_downloads_it_first(records):
    from ui_qt.widgets.past_meetings_panel import PastMeetingItem, PastMeetingsPanel

    records.remote["meeting"] = [{
        "id": "m_remote000001", "title": "Planning", "status": "ended",
        "started_at": datetime(2026, 9, 1, tzinfo=timezone.utc).isoformat(),
        "ended_at": datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc).isoformat(),
        "paused_total_s": 0, "cloud_enabled": False, "asr_model": "parakeet-v3",
        "state_json": "{}", "content_summary": {"has_audio": True, "has_transcript": True},
    }]
    panel = PastMeetingsPanel()
    panel.refresh()

    def cards():
        return [c for c in panel.findChildren(PastMeetingItem) if c.meeting_id == "m_remote000001"]

    assert _pump(lambda: bool(cards()))
    card = cards()[0]
    assert card.location_pill.text() == "On devbox"
    opened = []
    panel.meeting_selected.connect(opened.append)
    panel._on_card_selected("m_remote000001")
    assert _pump(lambda: opened == ["m_remote000001"])
    assert records.checked_out == [("meeting", "m_remote000001")]
    # Let the refresh that follows a fetch land before the panel goes.
    generation = panel._meeting_load_generation
    _pump(lambda: False, timeout=0.6)
    assert panel._meeting_load_generation == generation
    panel.deleteLater()
