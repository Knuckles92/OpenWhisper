"""Review round 2 for the transcription runtime: cancel, previews and the job slot."""

import threading
import time
from types import SimpleNamespace

import pytest
from PyQt6.QtTest import QTest

from services.automation.control import ControlError
from services.dictation_pipeline import DictationJob, JobMode
from services.runtime import transcription
from tests import test_automation_foundation_pipeline as foundation

# The foundation's harness fixtures, used here by name.
_qapp = foundation._qapp
h = foundation.h
_events = foundation._events


class Recorder:
    """The recorder's surface a start, stop and cancel use."""

    capture_canceled = False

    def __init__(self, *, recording=False):
        self.is_recording = recording

    def start_recording(self):
        self.is_recording = True
        return True

    def stop_recording(self):
        return True  # post-roll keeps it recording a little longer

    def cancel_recording(self):
        self.is_recording = False

    def get_recording_duration(self):
        return 1.5


# -- finding 1: the transcription runtime says whether it canceled anything ----------


def test_cancel_reports_a_recording_or_job_it_canceled(h):
    h.controller.recorder = Recorder(recording=True)
    h.controller.recording_state_changed = foundation.Signal()
    assert h.runtime.cancel() is True

    assert h.runtime._claim_job(DictationJob())
    h.controller.current_backend.is_transcribing = False
    assert h.runtime.cancel() is True


def test_cancel_with_nothing_going_on_reports_nothing_canceled(h):
    h.controller.current_backend.is_transcribing = False
    assert h.runtime.cancel() is False
    assert h.controller.status_update.calls[-1] == ("Canceled",)


# -- finding 5: no preview follows the end of its recording ---------------------------


@pytest.fixture
def recording(h, monkeypatch):
    """Starts recordings through the runtime, as the record shortcut does."""
    monkeypatch.setattr(transcription, "audio_player", SimpleNamespace(stop_playback=lambda: None))
    h.controller.recorder = Recorder()
    h.controller.recording_state_changed = foundation.Signal()
    h.controller._streaming_enabled = False
    h.ui.clear_transcription_stats = lambda: None

    def start():
        assert h.runtime.start_recording(source="hotkey")
        return h.runtime._job

    return start


def _timeline(h):
    return [(e.type, e.data.get("text")) for e in h.seen
            if e.type.startswith("recording.") or e.type == "transcription.preview"]


@pytest.mark.parametrize("end, event", [
    (lambda runtime: runtime.stop_recording(), "recording.stopped"),
    (lambda runtime: runtime.cancel(), "recording.canceled"),
], ids=["stop", "cancel"])
def test_no_preview_follows_the_end_of_its_recording(h, recording, end, event):
    from services.application_controller import PreviewEvents

    previews = PreviewEvents(h.runtime.publish_preview)
    job = recording()
    previews.offer(job, "hello", False)        # goes out at once
    previews.offer(job, "hello wor", False)    # held for a quarter second
    end(h.runtime)
    previews.offer(job, "hello world", False)  # one still on its way
    _events(h, event)
    QTest.qWait(400)

    assert _timeline(h) == [
        ("recording.started", None), ("transcription.preview", "hello"), (event, None),
    ]


def test_a_held_preview_never_reaches_the_next_recording(h, recording):
    from services.application_controller import PreviewEvents

    previews = PreviewEvents(h.runtime.publish_preview)
    first = recording()
    previews.offer(first, "hello", False)
    previews.offer(first, "hello wor", False)  # held
    h.runtime.stop_recording()
    h.controller.recorder.is_recording = False  # post-roll ended
    h.runtime._finish_job()  # and it was transcribed meanwhile
    second = recording()
    _events(h, "recording.started")
    QTest.qWait(400)

    assert [e.data["job_id"] for e in h.seen if e.type == "transcription.preview"] == [first.id]
    previews.offer(second, "next", False)
    QTest.qWait(400)
    assert [e.data["job_id"] for e in h.seen if e.type == "transcription.preview"][-1] == second.id


def test_previews_are_live_only(h, recording):
    job = recording()
    h.runtime.publish_preview(job, "hello", False)
    _events(h, "transcription.preview")
    assert [e.type for e in h.bus.recent()] == ["recording.started"]


# -- finding 10: a rewrite never waits on a recording that is starting ---------------


def _hold_capture_lock(h, seconds):
    """A record start on the hotkey thread, holding the capture lock while
    the audio device opens; returns the thread once it holds it."""
    holding = threading.Event()

    def start():
        with h.runtime._capture_lock:
            holding.set()
            time.sleep(seconds)

    starter = threading.Thread(target=start, daemon=True)
    starter.start()
    assert holding.wait(5)
    return starter


def test_an_api_run_is_refused_at_once_while_a_recording_starts(h):
    starter = _hold_capture_lock(h, 1.0)
    began = time.monotonic()
    with pytest.raises(ControlError) as busy:
        h.runtime.run_action_text("a1", "hello", source="api")
    waited = time.monotonic() - began
    starter.join(5)

    assert busy.value.code == "busy"
    assert waited < 0.5
    assert not h.runtime.has_active_job


def test_a_transform_is_refused_at_once_while_a_recording_starts(h):
    starter = _hold_capture_lock(h, 1.0)
    began = time.monotonic()
    began_job = h.runtime.begin_rewrite_job(
        DictationJob(mode=JobMode.TRANSFORM), source_name="Transform · Polish")
    waited = time.monotonic() - began
    starter.join(5)

    assert began_job is False and waited < 0.5
    assert h.ui.notices[-1] == "A transcription is already in progress — wait before rewriting text"
    # Once the start is over, a rewrite gets the slot as before.
    assert h.runtime.begin_rewrite_job(
        DictationJob(mode=JobMode.TRANSFORM), source_name="Transform · Polish")
