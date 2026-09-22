"""Keep clipboard snapshotting and history persistence off the paste path.

The 2026-09-22 latency audit found two avoidable waits between a finished
transcript and the paste keystroke: copying the user's previous clipboard
(100-263 ms with a screenshot on it) and saving the history entry (5-8 ms
installed, ~90 ms on a cold source run). The snapshot is now taken while the
user speaks and history is saved after the paste; these tests pin both.
"""

import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QEventLoop, QMimeData, QTimer

from services.runtime import transcription
from services.runtime.transcription import TranscriptionRuntime
from services.settings import SettingsKey
from ui_qt.clipboard import ClipboardSnapshot, TemporaryClipboard


def _pump(ms=30):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class FakeSettings:
    def __init__(self):
        self.values = {SettingsKey.AUTO_PASTE: True, SettingsKey.COPY_CLIPBOARD: True}

    def load_all_settings(self):
        return dict(self.values)

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeHistory:
    def __init__(self, events):
        self.events = events
        self.entries = []
        self.error = None

    def add_entry(self, **kwargs):
        self.events.append("history")
        if self.error is not None:
            raise self.error
        self.entries.append(kwargs)


class FakeRecorder:
    def __init__(self, events):
        self.events = events
        self.is_recording = False
        self.start_ok = True
        self.last_start_error = "No audio device available"
        self.capture_canceled = False

    def start_recording(self):
        self.events.append("recorder_start")
        self.is_recording = self.start_ok
        self.capture_canceled = False
        return self.start_ok

    def stop_recording(self):
        self.is_recording = False
        return True

    def clear_recording_data(self):
        pass

    def cancel_recording(self):
        self.capture_canceled = True
        self.stop_recording()


class FakeUI:
    """Records the calls the runtime makes, in order, in ``events``."""

    def __init__(self, events):
        self.events = events
        self.statuses = []
        self.main_window = SimpleNamespace(clear_partial_transcription=lambda: None)
        self.stage_error = None

    def set_transcript(self, text, raw=None):
        pass

    def set_transcription_stats(self, *args, **kwargs):
        pass

    def clear_transcription_stats(self):
        pass

    def set_status(self, text):
        self.statuses.append(text)
        self.events.append(("status", text))

    def refresh_history(self):
        self.events.append("refresh_history")

    def prefetch_clipboard_snapshot(self):
        self.events.append("prefetch")

    def discard_clipboard_prefetch(self):
        self.events.append("discard_prefetch")

    def copy_to_clipboard(self, text):
        self.events.append("copy")
        return True

    def stage_transcript_for_paste(self, text):
        self.events.append("stage")
        if self.stage_error is not None:
            raise self.stage_error
        return SimpleNamespace(written=True, lease=object(), restore_unavailable=False)

    def schedule_clipboard_restore(self, stage):
        self.events.append("schedule_restore")
        return True

    def commit_transcript_clipboard(self, stage, text):
        self.events.append("commit")
        return True


class TemporaryClipboardUI(FakeUI):
    """FakeUI whose clipboard calls reach a real TemporaryClipboard."""

    def __init__(self, events, temporary):
        super().__init__(events)
        self.temporary = temporary

    def prefetch_clipboard_snapshot(self):
        self.temporary.request_prefetch(0)

    def discard_clipboard_prefetch(self):
        self.temporary.discard_prefetch()

    def stage_transcript_for_paste(self, text):
        self.events.append("stage")
        return self.temporary.stage_text(text)

    def schedule_clipboard_restore(self, stage):
        return self.temporary.schedule_restore(stage.lease, 0)


def _harness(monkeypatch, ui_factory=FakeUI):
    events = []
    ui = ui_factory(events)
    history = FakeHistory(events)
    settings = FakeSettings()
    paste = Mock(side_effect=lambda: events.append("paste"))
    monkeypatch.setattr(transcription, "history_manager", history)
    monkeypatch.setattr(transcription, "settings_manager", settings)
    monkeypatch.setattr(transcription, "send_paste", paste)
    monkeypatch.setattr(transcription, "is_accessibility_trusted", lambda: True)
    overlay = Mock()
    overlay.emit.side_effect = lambda state: events.append(("overlay", state))
    controller = SimpleNamespace(
        ui_controller=ui,
        recorder=FakeRecorder(events),
        streaming_runtime=Mock(),
        is_meeting_active=lambda: False,
        overlay_state_update=overlay,
        status_update=Mock(),
        recording_state_changed=Mock(),
        _pending_audio_path="recording.wav",
        _pending_audio_duration=10.0,
        _pending_file_size=4096,
        _pending_source_name="Quick Record",
        _pending_streaming_text="",
        _transcription_elapsed=0.1,
        _transcription_start_time=None,
    )
    runtime = TranscriptionRuntime(controller)
    monkeypatch.setattr(runtime, "_model_info_for_history", lambda: "parakeet")
    return SimpleNamespace(
        events=events,
        ui=ui,
        history=history,
        settings=settings,
        paste=paste,
        controller=controller,
        runtime=runtime,
    )


@pytest.fixture
def h(monkeypatch):
    harness = _harness(monkeypatch)
    assert harness.runtime._claim_job()
    return harness


# --- #5: paste before history ----------------------------------------------


def test_paste_happens_before_history_is_saved(h):
    h.runtime.on_transcription_complete("hello world")

    events = h.events
    assert events.index("stage") < events.index("paste") < events.index("history")
    assert events.index("history") < events.index("refresh_history")
    # History reads the one-shot metadata before it is cleared.
    entry = h.history.entries[0]
    assert entry["text"] == "hello world"
    assert entry["source_audio_path"] == "recording.wav"
    assert entry["audio_duration"] == 10.0
    assert entry["source_name"] == "Quick Record"
    assert h.controller._pending_audio_path is None
    assert h.controller._pending_source_name is None
    assert not h.runtime.has_active_job


def test_paste_status_stays_the_final_status(h):
    h.runtime.on_transcription_complete("hello world")

    statuses = [e for e in h.events if isinstance(e, tuple) and e[0] == "status"]
    assert statuses == [("status", "Ready (Pasted)")]


def test_history_failure_still_pastes(h, caplog):
    h.history.error = RuntimeError("database is locked")

    with caplog.at_level(logging.ERROR, logger=transcription.logger.name):
        h.runtime.on_transcription_complete("hello world")

    assert h.paste.called
    assert h.ui.statuses[-1] == "Ready (Pasted)"
    assert "Failed to save transcription to history: database is locked" in caplog.text
    assert h.controller._pending_audio_path is None
    assert not h.runtime.has_active_job


def test_paste_failure_still_records_history(h):
    h.paste.side_effect = RuntimeError("blocked")

    h.runtime.on_transcription_complete("hello world")

    assert "commit" in h.events
    assert [entry["text"] for entry in h.history.entries] == ["hello world"]
    assert h.ui.statuses[-1] == "Transcription complete (paste failed)"
    assert not h.runtime.has_active_job


def test_unexpected_clipboard_error_still_records_history_and_frees_the_job(h):
    h.ui.stage_error = RuntimeError("clipboard exploded")

    with pytest.raises(RuntimeError, match="clipboard exploded"):
        h.runtime.on_transcription_complete("hello world")

    assert [entry["text"] for entry in h.history.entries] == ["hello world"]
    assert h.controller._pending_audio_path is None
    assert not h.runtime.has_active_job


def test_upload_saves_history_before_ready_and_never_pastes(h):
    h.runtime._deliver_to_clipboard = False

    h.runtime.on_transcription_complete("uploaded words")

    delivery = [e for e in h.events if not (isinstance(e, tuple) and e[0] == "overlay")]
    assert delivery == ["history", "refresh_history", ("status", "Ready")]
    assert not h.paste.called
    assert not h.runtime.has_active_job


def test_empty_result_skips_history_and_paste_and_drops_the_prefetch(h):
    h.runtime.on_transcription_complete("   ")

    assert h.history.entries == []
    assert not h.paste.called
    assert "discard_prefetch" in h.events
    assert not h.runtime.has_active_job


def test_copy_only_delivery_drops_the_prefetch(h):
    h.settings.values[SettingsKey.AUTO_PASTE] = False

    h.runtime.on_transcription_complete("hello world")

    assert not h.paste.called
    assert h.events.index("discard_prefetch") < h.events.index("copy")
    assert h.ui.statuses[-1] == "Ready"
    assert len(h.history.entries) == 1


def test_transcription_error_drops_the_prefetch(h):
    h.runtime.on_transcription_error("backend unavailable")

    assert "discard_prefetch" in h.events
    assert not h.runtime.has_active_job


# --- #4: snapshot prefetch lifecycle ----------------------------------------


def test_recording_start_requests_one_prefetch_after_the_recorder_and_overlay(
    monkeypatch,
):
    h = _harness(monkeypatch)

    assert h.runtime.start_recording()

    events = h.events
    assert events.count("prefetch") == 1
    assert events.index("recorder_start") < events.index("prefetch")
    overlay = next(i for i, e in enumerate(events) if isinstance(e, tuple) and e[0] == "overlay")
    assert overlay < events.index("prefetch")


def test_no_prefetch_when_auto_paste_is_off(monkeypatch):
    h = _harness(monkeypatch)
    h.settings.values[SettingsKey.AUTO_PASTE] = False

    assert h.runtime.start_recording()

    assert "prefetch" not in h.events


def test_failed_start_requests_no_prefetch_and_drops_any_stale_one(monkeypatch):
    h = _harness(monkeypatch)
    h.controller.recorder.start_ok = False

    assert not h.runtime.start_recording()

    assert "prefetch" not in h.events
    assert "discard_prefetch" in h.events


def test_refused_start_touches_no_clipboard(monkeypatch):
    h = _harness(monkeypatch)
    h.controller.is_meeting_active = lambda: True

    assert not h.runtime.start_recording()

    assert "prefetch" not in h.events
    assert "recorder_start" not in h.events


def test_cancel_during_recording_drops_the_prefetch(monkeypatch):
    h = _harness(monkeypatch)
    assert h.runtime.start_recording()

    h.runtime.cancel()

    assert h.events.index("prefetch") < h.events.index("discard_prefetch")


# --- #4 end to end through a real TemporaryClipboard -------------------------


class SequencedClipboard:
    """In-memory clipboard with a Windows-style change counter."""

    def __init__(self, text):
        self.counter = 1
        self._mime_data = QMimeData()
        self._mime_data.setText(text)

    def sequence(self):
        return self.counter

    def mimeData(self):
        return self._mime_data

    def setMimeData(self, mime_data):
        self._mime_data = mime_data
        self.counter += 1

    def setText(self, text):
        mime_data = QMimeData()
        mime_data.setText(text)
        self.setMimeData(mime_data)

    def text(self):
        return self._mime_data.text()


@pytest.fixture
def captures(monkeypatch):
    calls = []
    original = ClipboardSnapshot.capture.__func__

    def counting_capture(cls, mime_data):
        calls.append(threading.current_thread())
        return original(cls, mime_data)

    monkeypatch.setattr(ClipboardSnapshot, "capture", classmethod(counting_capture))
    return calls


def _clipboard_harness(monkeypatch, text="user's own clipboard"):
    clipboard = SequencedClipboard(text)
    temporary = TemporaryClipboard(clipboard, sequence_source=clipboard.sequence)
    harness = _harness(
        monkeypatch, lambda events: TemporaryClipboardUI(events, temporary)
    )
    harness.clipboard = clipboard
    return harness


def _start_from_hotkey_thread(runtime):
    started = []
    worker = threading.Thread(target=lambda: started.append(runtime.start_recording()))
    worker.start()
    worker.join()
    assert started == [True]


def test_dictation_pastes_without_copying_the_clipboard_again(monkeypatch, captures):
    h = _clipboard_harness(monkeypatch)
    _start_from_hotkey_thread(h.runtime)
    _pump()
    # Captured once, on the Qt thread, while "recording".
    assert captures == [threading.main_thread()]

    h.controller.recorder.is_recording = False
    assert h.runtime._claim_job()
    h.runtime.on_transcription_complete("dictated words")

    assert len(captures) == 1
    assert h.paste.called
    assert h.clipboard.text() == "dictated words"
    _pump()
    assert h.clipboard.text() == "user's own clipboard"


def test_copy_made_during_dictation_is_what_gets_restored(monkeypatch, captures):
    h = _clipboard_harness(monkeypatch)
    _start_from_hotkey_thread(h.runtime)
    _pump()

    h.clipboard.setText("copied mid-dictation")
    h.controller.recorder.is_recording = False
    assert h.runtime._claim_job()
    h.runtime.on_transcription_complete("dictated words")
    _pump()

    assert len(captures) == 2
    assert h.clipboard.text() == "copied mid-dictation"


def test_canceled_dictation_leaves_no_snapshot_behind(monkeypatch, captures):
    h = _clipboard_harness(monkeypatch)
    _start_from_hotkey_thread(h.runtime)
    _pump()
    h.runtime.cancel()
    _pump()

    h.ui.temporary.stage_text("next transcript")

    assert len(captures) == 2
