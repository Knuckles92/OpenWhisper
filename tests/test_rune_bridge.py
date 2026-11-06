"""The native desktop cannot write arbitrary settings or bypass busy guards."""

import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from services.rune_bridge import MAX_REQUEST_BYTES, RuneBridge


class Events(QObject):
    status_changed = pyqtSignal(str)
    transcription_received = pyqtSignal(str, object)


class Controller:
    def __init__(self):
        self.ui_controller = Events()
        self.transcription_runtime = SimpleNamespace(
            has_active_job=False, current_job=None,
        )
        self.recorder = SimpleNamespace(is_recording=False)
        self._startup_database_ready = True
        self.is_meeting_active = lambda: False
        self.upload_audio_file = Mock()


@pytest.fixture
def bridge():
    return RuneBridge(Controller(), io.StringIO())


@pytest.mark.parametrize("key,value", [
    ("api_token", "a new key"), ("auto_paste", "true"),
    ("local_asr_language", "../../bad"), ("transcript_cleanup_enabled", 1),
])
def test_rejects_settings_outside_the_native_contract(bridge, monkeypatch, key, value):
    from services.settings import settings_manager

    save = Mock()
    monkeypatch.setattr(settings_manager, "save_setting", save)
    with pytest.raises(ValueError, match="cannot be changed"):
        bridge.dispatch({"method": "settings.update", "params": {"key": key, "value": value}})
    save.assert_not_called()


def test_settings_and_file_import_refuse_during_recording(bridge, monkeypatch, tmp_path):
    from services.settings import settings_manager

    save = Mock()
    monkeypatch.setattr(settings_manager, "save_setting", save)
    bridge.controller.recorder.is_recording = True
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"fixture")
    for request in [
        {"method": "file.transcribe", "params": {"path": str(audio)}},
        {"method": "settings.update", "params": {"key": "auto_paste", "value": False}},
    ]:
        with pytest.raises(ValueError, match="Finish the current"):
            bridge.dispatch(request)
    save.assert_not_called()
    bridge.controller.upload_audio_file.assert_not_called()


def test_snapshot_waits_for_database_and_omits_credentials(bridge, monkeypatch):
    from services.database import db
    from services.settings import settings_manager

    query = Mock(return_value=[])
    monkeypatch.setattr(db, "get_history_entries", query)
    monkeypatch.setattr(settings_manager, "load_all_settings", lambda: {
        "auto_paste": False, "openai_api_key": "fixture-secret", "api_token": "fixture-token",
    })
    monkeypatch.setattr(settings_manager, "load_model_selection", lambda: "local_whisper")
    bridge.controller._startup_database_ready = False
    snapshot = bridge.snapshot()
    query.assert_not_called()
    assert snapshot["history"] == []
    assert not snapshot["settings"]["auto_paste"]
    assert "fixture-secret" not in json.dumps(snapshot)
    assert "fixture-token" not in json.dumps(snapshot)
    bridge.controller._startup_database_ready = True
    bridge.snapshot()
    query.assert_called_once_with(limit=60)
    bridge.snapshot()
    query.assert_called_once_with(limit=60)


def test_protocol_recovers_after_invalid_json_and_preserves_request_ids(bridge, monkeypatch):
    from services.automation import app_control

    monkeypatch.setattr(app_control, "call_on_qt", lambda fn: fn())
    monkeypatch.setattr(bridge, "dispatch", lambda req: {"action": req["method"]})
    stdin = SimpleNamespace(buffer=io.BytesIO(
        b'not json\n{"id":7,"method":"snapshot"}\n'
    ))
    monkeypatch.setattr("sys.stdin", stdin)
    bridge._serve()
    replies = [json.loads(line) for line in bridge.output.getvalue().splitlines()]
    assert len(replies) == 2
    assert "error" in replies[0]
    assert replies[1] == {"id": 7, "result": {"action": "snapshot"}}


def test_oversized_request_closes_transport_without_executing(bridge, monkeypatch):
    from services.automation import app_control

    monkeypatch.setattr(app_control, "call_on_qt", lambda fn: fn())
    dispatch = Mock()
    monkeypatch.setattr(bridge, "dispatch", dispatch)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"x" * (MAX_REQUEST_BYTES + 1))))
    bridge._serve()
    dispatch.assert_not_called()
    assert "too large" in json.loads(bridge.output.getvalue())["error"]


def test_transcript_signal_reaches_the_snapshot_model(bridge):
    bridge.controller.ui_controller.transcription_received.emit("Tidied words", "spoken words")
    bridge.controller.ui_controller.status_changed.emit("Ready")
    assert (bridge.transcript, bridge.raw, bridge.status) == ("Tidied words", "spoken words", "Ready")
