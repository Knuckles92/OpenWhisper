"""Review round 4, F2 and F4: what a Cancel does to the job in progress, and says.

F2: only a job's first Cancel publishes and shows anything; a repeat while
it winds down is quiet, and the API refuses it. Every canceled job ends the
same way: ``recording.canceled`` when the Cancel lands, then, for a job that
had taken the slot (a stopped recording, an upload, a rewrite),
``transcription.failed`` ``canceled`` when it lets the slot go.

F4: a Cancel that canceled nothing never says "Canceled".
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

from config import config
from services.automation import app_control, catalog
from services.automation.control import ControlError
from services.dictation_pipeline import DictationJob, JobMode
from services.runtime import transcription
from tests import test_automation_foundation_pipeline as foundation

_qapp = foundation._qapp
h = foundation.h


class Recorder:
    """The recorder's surface a start, stop and cancel use.

    Like the real one, a canceled capture still reads as recording until its
    stream closes (``closed``), and ``capture_canceled`` says it was thrown away.
    """

    def __init__(self):
        self.is_recording = False
        self.capture_canceled = False
        self.last_capture_error = None
        self.last_start_error = None

    def start_recording(self):
        self.is_recording, self.capture_canceled = True, False
        return True

    def stop_recording(self):
        return True  # post-roll: still recording until the worker waits it out

    def cancel_recording(self):
        self.capture_canceled = True

    def closed(self):
        self.is_recording = False

    def get_recording_duration(self):
        return 2.5

    def wait_for_stop_completion(self):
        self.is_recording = False
        return True

    def has_recording_data(self):
        return True

    def save_recording(self, allow_incomplete=False):
        os.makedirs(os.path.dirname(config.RECORDED_AUDIO_FILE), exist_ok=True)
        with open(config.RECORDED_AUDIO_FILE, "wb") as file:
            file.write(b"RIFF" + bytes(400))
        return True

    def acknowledge_recording(self):
        pass


@pytest.fixture
def app(h, monkeypatch):
    monkeypatch.setattr(transcription, "audio_player", SimpleNamespace(stop_playback=lambda: None))
    controller = h.controller
    controller.recorder = Recorder()
    controller.recording_state_changed = foundation.Signal()
    controller.streaming_overlay_hide = foundation.Signal()
    controller._streaming_enabled = False
    controller.current_backend.is_transcribing = False
    controller.current_backend.is_remote = False
    h.ui.clear_transcription_stats = lambda: None
    h.ui.prefetch_clipboard_snapshot = lambda: None
    controller.streaming_runtime.stop_streaming_session.return_value = ""
    controller.streaming_runtime.finalize_streaming_text.return_value = ""
    return h


def kinds(h):
    """What was published, in order, with a failure's category."""
    return [e.type + (f"({e.data['error']})" if e.type == catalog.TRANSCRIPTION_FAILED else "")
            for e in h.bus.recent(500)]


def run_worker(h):
    while h.submitted:
        fn, args = h.submitted.pop(0)
        fn(*args)


def shown(h):
    """What the runtime has shown so far: statuses and overlay states."""
    return list(h.controller.status_update.calls), list(h.controller.overlay_state_update.calls)


STOPPED = [catalog.RECORDING_STARTED, catalog.RECORDING_STOPPED]
CANCELED = [catalog.RECORDING_CANCELED, f"{catalog.TRANSCRIPTION_FAILED}(canceled)"]


# -- F2: a repeat does nothing, in every phase -------------------------------------------


def test_a_second_cancel_while_recording_is_quiet(app):
    h = app
    assert h.runtime.start_recording()
    assert h.runtime.cancel() is True
    before = shown(h)

    # The capture's stream is still closing.
    assert h.runtime.cancelable is False
    assert h.runtime.cancel() is True
    assert shown(h) == before
    h.controller.recorder.closed()

    # A recording canceled before it stopped ends with recording.canceled.
    assert kinds(h) == [catalog.RECORDING_STARTED, catalog.RECORDING_CANCELED]
    assert h.runtime.cancel() is False


def test_a_second_cancel_in_post_roll_is_quiet(app):
    h = app
    assert h.runtime.start_recording()
    h.runtime.stop_recording()  # the slot is claimed; post-roll goes on
    assert h.runtime.cancel() is True
    before = shown(h)

    assert h.runtime.cancelable is False
    assert h.runtime.cancel() is True
    assert shown(h) == before
    run_worker(h)

    assert kinds(h) == STOPPED + CANCELED
    assert not h.runtime.has_active_job


def test_a_second_cancel_before_transcription_is_quiet(app):
    h = app
    assert h.runtime.start_recording()
    h.runtime.stop_recording()
    h.controller.recorder.is_recording = False  # post-roll over; the WAV is next
    assert h.runtime.cancel() is True
    before = shown(h)

    assert h.runtime.cancelable is False
    assert h.runtime.cancel() is True
    assert shown(h) == before
    run_worker(h)

    assert kinds(h) == STOPPED + CANCELED
    assert not h.runtime.has_active_job


def test_a_second_cancel_while_transcribing_is_quiet(app):
    h = app
    backend = h.controller.current_backend
    started, release = threading.Event(), threading.Event()
    stops = []

    def transcribe(path):
        backend.is_transcribing = True
        started.set()
        release.wait(5)
        backend.is_transcribing = False
        if stops:
            raise RuntimeError("Transcription canceled")
        return "hello there"

    backend.transcribe = transcribe
    backend.cancel_transcription = lambda: stops.append(True)
    assert h.runtime.start_recording()
    h.runtime.stop_recording()
    fn, args = h.submitted.pop(0)
    worker = threading.Thread(target=fn, args=args)
    worker.start()
    try:
        assert started.wait(5)
        assert h.runtime.cancel() is True
        before = shown(h)

        assert h.runtime.cancelable is False
        assert h.runtime.cancel() is True
        assert shown(h) == before
        assert stops == [True]
    finally:
        release.set()
        worker.join(5)

    assert kinds(h) == STOPPED + CANCELED
    assert not h.runtime.has_active_job


def test_the_api_refuses_a_second_cancel(app):
    h = app
    assert h.runtime.start_recording()
    h.runtime.stop_recording()
    controller = SimpleNamespace(recorder=h.controller.recorder, transcription_runtime=h.runtime)

    app_control._cancel(controller)
    with pytest.raises(ControlError) as refused:
        app_control._cancel(controller)

    assert refused.value.code == "refused"
    run_worker(h)
    assert kinds(h) == STOPPED + CANCELED


def test_a_rewrite_canceled_while_it_reads_the_selection_ends_canceled(app):
    h = app
    job = DictationJob(mode=JobMode.TRANSFORM, source="hotkey")
    assert h.runtime.begin_rewrite_job(job, source_name="Transform · Shout")

    assert h.runtime.cancel() is True
    h.runtime.abandon_rewrite_job("Select text to transform")

    assert kinds(h) == CANCELED
    failed, = [e.data for e in h.bus.recent(types=[catalog.TRANSCRIPTION_FAILED])]
    assert failed["job_id"] == job.id
    assert not h.runtime.has_active_job


# -- F4: a Cancel that canceled nothing never says "Canceled" -----------------------------


def test_a_cancel_after_an_upload_was_delivered_says_nothing(h):
    h.saves = []
    h.controller.persistence_executor = SimpleNamespace(
        submit=lambda fn, *args: h.saves.append((fn, args)))
    h.controller.current_backend.is_transcribing = False
    h.runtime.upload_audio_file(h.audio)
    fn, args = h.submitted.pop(0)
    fn(*args)  # delivered; its history is being saved
    before = shown(h)

    assert h.runtime.cancel() is False

    assert shown(h) == before
    assert catalog.RECORDING_CANCELED not in kinds(h)


def users_cancel(h, actions_reached=0):
    """ApplicationController.cancel, with its signal calling the slot at once."""
    from services.application_controller import ApplicationController

    controller = SimpleNamespace(
        transcription_runtime=h.runtime, ui_controller=h.ui,
        action_runtime=SimpleNamespace(cancel=lambda: actions_reached))
    controller.actions_cancel_requested = SimpleNamespace(
        emit=lambda: ApplicationController._cancel_actions(controller))
    ApplicationController.cancel(controller)


def test_cancel_while_a_pasted_dictation_is_saved_says_there_was_nothing_to_cancel(h):
    h.saves = []
    h.controller.persistence_executor = SimpleNamespace(
        submit=lambda fn, *args: h.saves.append((fn, args)))
    h.controller.current_backend.is_transcribing = False
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION, source="hotkey"))
    h.runtime.transcribe_audio_file(h.audio)
    assert h.ui.stages and h.saves  # pasted; its history is being saved
    statuses, overlays = shown(h)

    users_cancel(h)

    assert shown(h) == (statuses, overlays)  # no "Canceled", no cancel animation
    assert h.ui.statuses[-1] == "Nothing to cancel"
    assert h.ui.notices[-1] == "Nothing to cancel"
    while h.saves:
        fn, args = h.saves.pop(0)
        fn(*args)
    assert len(h.history.entries) == 1


def test_cancel_with_nothing_at_all_says_so(h):
    h.controller.current_backend.is_transcribing = False

    users_cancel(h)

    assert shown(h) == ([], [])
    assert h.ui.statuses == ["Nothing to cancel"]


def test_cancel_that_reached_actions_leaves_them_to_say_what_it_did(h):
    h.controller.current_backend.is_transcribing = False

    users_cancel(h, actions_reached=1)

    assert h.ui.statuses == [] and h.ui.notices == []
