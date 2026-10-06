"""An agent's retitle reaches the copy the paired host keeps."""

import importlib
from types import SimpleNamespace

from PyQt6.QtCore import QObject

from ui_qt.ui_controller import UIController


class _Sync:
    def __init__(self):
        self.calls = []

    def record_saved(self, kind, record_id):
        self.calls.append(("saved", kind, record_id))

    def record_edited(self, kind, record_id):
        self.calls.append(("edited", kind, record_id))


def test_a_retitled_transcription_is_queued_for_its_host_copy(monkeypatch):
    sync_module = importlib.import_module("services.remote_records.sync")
    fake = _Sync()
    monkeypatch.setattr(sync_module.record_sync, "_instance", fake)
    ui = UIController.__new__(UIController)
    QObject.__init__(ui)
    refreshed = []
    ui.main_window = SimpleNamespace(refresh_history=lambda: refreshed.append(True))

    ui._apply_agent_changes("transcription", {"id": "e1", "title": "Standup"})

    assert refreshed == [True]
    assert fake.calls == [("saved", "dictation", "e1"), ("edited", "dictation", "e1")]
