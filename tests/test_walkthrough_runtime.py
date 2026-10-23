"""A walkthrough job in TranscriptionRuntime: recorded like a dictation,
transcribed section by section, and delivered as a document."""

import dataclasses
import wave
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PyQt6.QtGui import QColor, QImage

from services import dictation_pipeline, focus_context
from services.dictation_pipeline import JobMode
from services.runtime import transcription
from services.runtime.transcription import TranscriptionRuntime
from services.settings import SettingsKey
from services.walkthrough import finish
from services.walkthrough.session import WalkthroughResult, WalkthroughSession
from tests.fakes.settings import InMemorySettings
from ui_qt.overlay_state import OverlayState

RATE = 16_000


class Signal:
    """A pyqtSignal stand-in that delivers at once, like a direct connection."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def emit(self, *args):
        self.calls.append(args)
        if self.handler is not None:
            self.handler(*args)


class FakeUI:
    def __init__(self):
        self.statuses = []
        self.copied_files = []
        self.ready = []
        self.transcripts = []
        self.prefetches = 0
        self.main_window = SimpleNamespace(clear_partial_transcription=lambda: None)

    def set_transcript(self, text, raw=None):
        self.transcripts.append((text, raw))

    def show_transcription_error(self, message):
        pass

    def set_transcription_stats(self, *args, **kwargs):
        pass

    def clear_transcription_stats(self):
        pass

    def set_status(self, text):
        self.statuses.append(text)

    def show_notice(self, text, *, done=False):
        pass

    def refresh_history(self):
        pass

    def prefetch_clipboard_snapshot(self):
        self.prefetches += 1

    def discard_clipboard_prefetch(self):
        pass

    def copy_files_to_clipboard(self, paths, text="", *, image=None):
        self.copied_files.append((list(paths), text))
        return True

    def show_walkthrough_ready(self, result, *, copied):
        self.ready.append((result, copied))

    def dismiss_walkthrough_panel(self):
        self.dismissed = getattr(self, "dismissed", 0) + 1


class FakeRecorder:
    def __init__(self):
        self.is_recording = False
        self.capture_canceled = False
        self.start_ok = True
        self.stop_ok = True
        self.duration = 0.0

    def start_recording(self):
        self.is_recording = self.start_ok
        self.capture_canceled = False
        return self.start_ok

    def stop_recording(self):
        self.is_recording = False
        return self.stop_ok

    def cancel_recording(self):
        self.capture_canceled = True
        self.is_recording = False

    def get_recording_duration(self):
        return self.duration


class FakeHistory:
    def __init__(self):
        self.entries = []

    def add_entry(self, **fields):
        self.entries.append(fields)
        return SimpleNamespace(id=f"entry-{len(self.entries)}", audio_file=None)


class FakeBackend:
    """Answers each section's clip with the words said in it."""

    def __init__(self, words=("first part", "second part", "last part")):
        self.words = list(words)
        self.paths = []

    def transcribe(self, path):
        self.paths.append(Path(path).name)
        return self.words[len(self.paths) - 1]

    def is_available(self):
        return True


def _tone(seconds):
    t = np.arange(int(seconds * RATE)) / RATE
    return (8000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


def _write_recording(path, pieces):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(np.concatenate(pieces).tobytes())


def _image():
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(QColor("#ff4f3a"))
    return image


@pytest.fixture
def h(monkeypatch, tmp_path):
    ui = FakeUI()
    history = FakeHistory()
    settings = InMemorySettings({SettingsKey.AUTO_PASTE: True, SettingsKey.COPY_CLIPBOARD: True})
    monkeypatch.setattr(transcription, "history_manager", history)
    monkeypatch.setattr(transcription, "settings_manager", settings)
    monkeypatch.setattr(transcription, "send_paste", Mock())
    backend = FakeBackend()
    controller = SimpleNamespace(
        ui_controller=ui,
        recorder=FakeRecorder(),
        streaming_runtime=Mock(),
        is_meeting_active=lambda: False,
        overlay_state_update=Signal(),
        status_update=Signal(),
        recording_state_changed=Signal(),
        recording_destination_changed=Signal(),
        streaming_overlay_hide=Signal(),
        transcription_completed=Signal(),
        transcription_failed=Signal(),
        executor=SimpleNamespace(submit=lambda fn, *args: None),
        persistence_executor=SimpleNamespace(submit=lambda fn, *args: fn(*args)),
        current_backend=backend,
        _streaming_enabled=False,
        _pending_audio_path=None,
        _pending_audio_duration=None,
        _pending_file_size=None,
        _pending_source_name=None,
        _pending_streaming_text="",
        _transcription_elapsed=None,
        _transcription_start_time=None,
        _remote_timing=None,
    )
    runtime = TranscriptionRuntime(controller)
    controller.history_persisted = Signal(runtime.on_history_persisted)
    controller.transcription_completed.handler = runtime.on_transcription_complete
    controller.transcription_failed.handler = runtime.on_transcription_error
    runtime._incremental = Mock()
    runtime._incremental.transcribe.side_effect = lambda backend, path: backend.transcribe(path)
    monkeypatch.setattr(runtime, "_model_info_for_history", lambda: "parakeet")
    built = []

    def build_documents(session, shots, texts, raws, *, duration_s):
        folder = session.folder()
        built.append(SimpleNamespace(shots=list(shots), texts=list(texts), duration_s=duration_s))
        return WalkthroughResult(
            folder=folder, pdf=folder / "walkthrough.pdf", markdown=folder / "walkthrough.md",
            clipboard_text="# Walkthrough (markdown)",
            history_text=finish.marked_text(texts, len(shots)),
            history_raw=None, shots=len(shots), duration_s=duration_s,
            first_shot=shots[0].path if shots else None,
        )

    monkeypatch.setattr(finish, "build_documents", build_documents)
    folder = tmp_path / "capture"

    def make_folder(started_at):
        folder.mkdir(exist_ok=True)
        return folder

    session = WalkthroughSession(started_at=datetime(2026, 10, 21, 14, 32), make_folder=make_folder)
    audio = tmp_path / "recording.wav"
    return SimpleNamespace(
        ui=ui, history=history, settings=settings, controller=controller, runtime=runtime,
        backend=backend, built=built, session=session, audio=audio,
    )


def _record(h, shots_at=(), *, length_s=6.0):
    """Start a walkthrough, keep a screenshot at each time, and stop it."""
    assert h.runtime.start_walkthrough(h.session)
    for kept_at in shots_at:
        h.controller.recorder.duration = kept_at
        assert h.session.add_shot(_image(), kept_at) is not None
    h.controller.recorder.duration = length_s
    h.runtime.stop_recording()
    # What finish_recording_job notes once the WAV is saved.
    h.controller._pending_audio_path = str(h.audio)
    h.controller._pending_audio_duration = length_s
    h.controller._pending_source_name = h.runtime._recording_source_name(h.runtime._active_job)


def test_a_walkthrough_records_like_a_dictation_without_early_decoding(h):
    assert h.runtime.start_walkthrough(h.session)

    job = h.runtime._job
    assert job.mode == JobMode.WALKTHROUGH
    assert h.runtime.recording_walkthrough is h.session
    assert h.controller.recording_destination_changed.calls == [("Walkthrough",)]
    assert h.controller.status_update.calls[-1] == ("Walkthrough · Recording...",)
    assert h.controller.overlay_state_update.calls[-1] == (OverlayState.RECORDING,)
    h.runtime._incremental.start.assert_not_called()
    # Nothing is pasted, so the user's clipboard isn't snapshotted for it.
    assert h.ui.prefetches == 0


def test_a_walkthrough_reads_nothing_on_screen(monkeypatch):
    service = Mock()
    monkeypatch.setattr(focus_context, "get_service", lambda: service)
    settings = {SettingsKey.APP_CONTEXT_ENABLED: True}

    job = dictation_pipeline.begin_job(JobMode.WALKTHROUGH, settings, source="hotkey")

    assert job.focus is None and job.target is None
    service.request.assert_not_called()


def test_stopping_closes_the_walkthrough_to_new_screenshots(h):
    _record(h, [1.0])

    assert not h.session.open
    assert h.runtime.recording_walkthrough is None
    assert h.runtime._active_job.mode == JobMode.WALKTHROUGH


def test_each_section_is_transcribed_and_the_document_copied(h):
    _write_recording(h.audio, [_tone(1.9), np.zeros(int(0.3 * RATE), np.int16), _tone(1.9),
                               np.zeros(int(0.3 * RATE), np.int16), _tone(1.6)])
    _record(h, [2.0, 4.2], length_s=6.0)

    h.runtime.transcribe_audio_file(str(h.audio))

    assert h.backend.paths == ["section-1.wav", "section-2.wav", "section-3.wav"]
    (built,) = h.built
    assert built.texts == ["first part", "second part", "last part"]
    assert [shot.kept_at_s for shot in built.shots] == [2.0, 4.2]
    assert built.duration_s == 6.0
    folder = h.session.folder()
    assert h.ui.copied_files == [([folder / "walkthrough.pdf"], "# Walkthrough (markdown)")]
    (result, copied), = h.ui.ready
    assert copied and result.shots == 2
    assert "Walkthrough copied (2 screenshots) — paste it into your agent" in h.ui.statuses
    (entry,) = h.history.entries
    assert entry["text"] == (
        "first part\n\n[Screenshot 1]\n\nsecond part\n\n[Screenshot 2]\n\nlast part"
    )
    assert entry["source_name"] == "Walkthrough"
    assert entry["entry_kind"] == "walkthrough"
    # The job is over: the session is let go, its files kept.
    assert h.runtime._walkthrough is None
    assert (folder / "shot-1.png").exists()


def test_each_section_is_cleaned_up_on_its_own(h, monkeypatch):
    _write_recording(h.audio, [_tone(5.0)])
    seen = []

    def cleanup(text, *args, **kwargs):
        seen.append(text)
        return text.upper(), text, None

    monkeypatch.setattr(h.runtime, "_maybe_cleanup_transcript", cleanup)
    _record(h, [2.5], length_s=5.0)

    h.runtime.transcribe_audio_file(str(h.audio))

    assert seen == ["first part", "second part"]
    assert h.built[0].texts == ["FIRST PART", "SECOND PART"]


def test_screenshots_without_words_still_make_a_walkthrough(h):
    _write_recording(h.audio, [np.zeros(4 * RATE, np.int16)])
    h.backend.words = ["", ""]
    _record(h, [2.0], length_s=4.0)

    h.runtime.transcribe_audio_file(str(h.audio))

    assert h.history.entries[0]["text"] == "[Screenshot 1]"
    assert h.ui.ready


def test_no_screenshots_and_no_words_is_no_speech(h):
    _write_recording(h.audio, [np.zeros(2 * RATE, np.int16)])
    h.backend.words = [""]
    _record(h, [], length_s=2.0)

    h.runtime.transcribe_audio_file(str(h.audio))

    assert h.history.entries == []
    assert h.ui.ready == []
    assert h.runtime._walkthrough is None
    # No empty document is written, and the column stops saying
    # "Writing it up…".
    assert h.built == []
    assert h.ui.dismissed == 1


def test_a_document_that_cannot_be_written_keeps_the_words(h, monkeypatch):
    import errno

    _write_recording(h.audio, [_tone(1.9), np.zeros(int(0.3 * RATE), np.int16), _tone(1.8)])
    _record(h, [2.0], length_s=4.0)

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(finish, "build_documents", disk_full)

    h.runtime.transcribe_audio_file(str(h.audio))

    (entry,) = h.history.entries
    assert entry["text"] == "first part\n\n[Screenshot 1]\n\nsecond part"
    assert h.ui.copied_files == [([], entry["text"])]
    assert h.ui.ready == []
    assert h.ui.dismissed == 1
    assert any(
        status.startswith("Couldn't save the walkthrough document (No space left on device)")
        and "copied and kept in History" in status
        for status in h.ui.statuses
    )


def test_screenshots_that_could_not_be_saved_are_named_in_the_status(h, monkeypatch):
    from services.walkthrough import session as session_module

    _write_recording(h.audio, [_tone(4.0)])
    monkeypatch.setattr(session_module, "_write_png", lambda image, path: False)
    _record(h, [2.0], length_s=4.0)

    h.runtime.transcribe_audio_file(str(h.audio))

    (result, _copied), = h.ui.ready
    assert result.lost_shots == 1
    assert any("1 screenshot couldn't be saved and is left out" in s for s in h.ui.statuses)


def test_canceling_a_walkthrough_deletes_its_screenshots(h):
    assert h.runtime.start_walkthrough(h.session)
    shot = h.session.add_shot(_image(), 1.0)
    h.session.saved_shots()
    h.controller.recorder.is_recording = True

    h.runtime.cancel()

    assert not shot.path.exists()
    assert h.runtime._walkthrough is None
    assert h.runtime.recording_walkthrough is None


def test_a_failed_walkthrough_keeps_its_screenshots_and_says_where(h):
    _write_recording(h.audio, [_tone(4.0)])
    _record(h, [2.0], length_s=4.0)
    h.backend.transcribe = Mock(side_effect=RuntimeError("engine stopped"))

    h.runtime.transcribe_audio_file(str(h.audio))

    folder = h.session.folder()
    assert any(f"screenshots kept in {folder}" in status for status in h.ui.statuses)
    assert (folder / "shot-1.png").exists()
    assert h.ui.copied_files == []


def test_a_cancel_between_sections_stops_the_walkthrough(h):
    _write_recording(h.audio, [_tone(4.0)])
    _record(h, [2.0], length_s=4.0)
    words = iter(["first part", "second part"])

    def transcribe(path):
        h.backend.paths.append(Path(path).name)
        h.runtime._cancel_requested.set()
        return next(words)

    h.backend.transcribe = transcribe
    folder = h.session.folder()

    h.runtime.transcribe_audio_file(str(h.audio))

    assert h.backend.paths == ["section-1.wav"]
    assert h.built == []
    assert h.ui.copied_files == []
    # Canceled after the recording stopped: thrown away all the same.
    assert not (folder / "shot-1.png").exists()
    assert not any("screenshots kept" in status for status in h.ui.statuses)


def _write_up_on(h, monkeypatch, write_up):
    from services.walkthrough import write_up as module

    h.settings.values[SettingsKey.WALKTHROUGH_WRITE_UP] = True
    h.settings.values[SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS] = "  Make it a checklist. "
    monkeypatch.setattr(module, "write_up", write_up)
    _write_recording(h.audio, [_tone(4.0)])
    _record(h, [2.0], length_s=4.0)


def test_a_walkthrough_written_up_is_what_gets_copied(h, monkeypatch):
    calls = []

    def write_up(result, shots, *, agent, instructions, follow_notes, cancel_event):
        calls.append((shots, agent, instructions, follow_notes, cancel_event))
        return dataclasses.replace(result, pdf=result.folder / "write-up.pdf",
                                   clipboard_text="# Checklist", written_up=True)

    h.settings.values[SettingsKey.WALKTHROUGH_WRITE_UP_AGENT] = "codex"
    h.settings.values[SettingsKey.WALKTHROUGH_WRITE_UP_NOTES] = True
    _write_up_on(h, monkeypatch, write_up)

    h.runtime.transcribe_audio_file(str(h.audio))

    folder = h.session.folder()
    ((shots, agent, instructions, follow_notes, cancel_event),) = calls
    assert shots == [folder / "shot-1.png"] and instructions == "Make it a checklist."
    assert follow_notes is True
    assert agent == "codex" and cancel_event is h.runtime._cancel_requested
    assert ("Writing it up with Codex...",) in h.controller.status_update.calls
    assert h.ui.copied_files == [([folder / "write-up.pdf"], "# Checklist")]
    assert "Write-up copied (1 screenshot) — paste it into your agent" in h.ui.statuses


def test_a_failed_write_up_copies_the_walkthrough_as_spoken(h, monkeypatch):
    from services.walkthrough.write_up import WriteUpFailed

    def write_up(result, shots, **kwargs):
        raise WriteUpFailed("Claude Code is not signed in.")

    _write_up_on(h, monkeypatch, write_up)

    h.runtime.transcribe_audio_file(str(h.audio))

    folder = h.session.folder()
    assert h.ui.copied_files == [([folder / "walkthrough.pdf"], "# Walkthrough (markdown)")]
    # The failure leads, so a status line cut short still says it.
    assert ("Write-up failed — Walkthrough copied as spoken (1 screenshot): "
            "Claude Code is not signed in.") in h.ui.statuses
    (result, _copied), = h.ui.ready
    assert result.write_up_error == "Claude Code is not signed in."


def test_a_cancel_during_the_write_up_stops_the_walkthrough(h, monkeypatch):
    from services.walkthrough.write_up import WriteUpFailed

    def write_up(result, shots, *, cancel_event, **kwargs):
        cancel_event.set()
        raise WriteUpFailed("canceled")

    _write_up_on(h, monkeypatch, write_up)
    folder = h.session.folder()

    h.runtime.transcribe_audio_file(str(h.audio))

    assert h.ui.copied_files == []
    assert not (folder / "shot-1.png").exists()


def test_an_unreadable_recording_is_transcribed_whole_above_the_screenshots(h):
    h.audio.write_bytes(b"not a wav file")
    h.backend.transcribe = Mock(return_value="everything I said")
    _record(h, [1.0, 2.0], length_s=3.0)

    h.runtime.transcribe_audio_file(str(h.audio))

    h.backend.transcribe.assert_called_once_with(str(h.audio))
    assert h.built[0].texts == ["everything I said", "", ""]


def test_walkthrough_text_reaches_events_with_its_raw_words():
    from services.automation import catalog

    job = dictation_pipeline.DictationJob(mode=JobMode.WALKTHROUGH)

    data = catalog.transcription_completed(job, text="Fixed.", raw_text="fixed")

    assert data["mode"] == "walkthrough"
    assert data["raw_text"] == "fixed"


def test_the_saved_job_future_is_never_needed():
    # A walkthrough job carries no captures for the paste check.
    job = dictation_pipeline.begin_job(JobMode.WALKTHROUGH, {}, source="hotkey")

    assert dictation_pipeline.paste_target(job) == dictation_pipeline.PasteTarget.UNKNOWN
    assert not isinstance(job.focus, Future)
