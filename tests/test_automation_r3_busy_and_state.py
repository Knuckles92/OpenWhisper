"""Review round 3: a program's refused run (F6) and the live state of a routed job (F7).

The real TranscriptionRuntime from start to delivery; the recorder, engine,
UI and routing's judge are fakes.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from config import config
from services.actions.model import Action, ActionKind
from services.automation.app_control import QtAppControl
from services.automation.control import ControlError
from services.dictation_pipeline import DictationJob, JobMode
from services.runtime import transcription
from services.settings import SettingsKey
from tests import test_automation_fix2_pipeline as fix2
from tests import test_automation_foundation_pipeline as foundation
from tests import test_automation_r2_program_words_pipeline as words

_qapp = foundation._qapp
h = foundation.h

CHANNEL = Action("ch", "Tell my session", ActionKind.CHANNEL)
BUSY = "A transcription is already in progress — wait before rewriting text"


# -- F6: a program's refused run says nothing to the user ---------------------------


def test_a_programs_run_while_the_user_records_tells_the_user_nothing(h):
    h.controller.recorder.is_recording = True

    with pytest.raises(ControlError) as busy:
        h.runtime.run_action_text("a1", "hello", source="api")

    assert busy.value.code == "busy"
    # The API's answer is the report; the user's "Recording..." stays.
    assert h.ui.notices == []
    assert h.controller.status_update.calls == []


def test_a_programs_run_while_a_recording_starts_tells_the_user_nothing(h):
    starter = fix2._hold_capture_lock(h, 0.5)
    with pytest.raises(ControlError) as busy:
        h.runtime.run_action_text("a1", "hello", source="mcp")
    starter.join(5)

    assert busy.value.code == "busy"
    assert h.ui.notices == [] and h.controller.status_update.calls == []


def test_the_users_own_rewrite_still_says_why_it_was_refused(h):
    h.controller.recorder.is_recording = True
    transform = DictationJob(mode=JobMode.TRANSFORM, source="hotkey")

    assert not h.runtime.begin_rewrite_job(transform, source_name="Transform · Polish")

    assert h.ui.notices == [BUSY]
    assert h.controller.status_update.calls == [(BUSY,)]


# -- F7: the live state shows the job as it is now ----------------------------------


class Recorder:
    """A recorder whose capture always works and saves a short WAV."""

    def __init__(self):
        self.is_recording = False
        self.capture_canceled = False
        self.last_capture_error = None
        self.last_start_error = None

    def start_recording(self):
        self.is_recording = True
        return True

    def stop_recording(self):
        self.is_recording = False
        return True

    def get_recording_duration(self):
        return 2.5

    def wait_for_stop_completion(self):
        return True

    def has_recording_data(self):
        return True

    def save_recording(self, allow_incomplete=False):
        os.makedirs(os.path.dirname(config.RECORDED_AUDIO_FILE), exist_ok=True)
        with open(config.RECORDED_AUDIO_FILE, "wb") as wav:
            wav.write(b"RIFF" + bytes(400))
        return True


@pytest.fixture
def app(h, monkeypatch):
    """Recordings through the runtime, as the record shortcut makes them;
    history saves are held, so the slot stays claimed after delivery."""
    monkeypatch.setattr(transcription, "audio_player", SimpleNamespace(stop_playback=lambda: None))
    c = h.controller
    c.recorder = Recorder()
    c.recording_state_changed = foundation.Signal()
    c.streaming_overlay_hide = foundation.Signal()
    c._streaming_enabled = False
    c.current_backend.is_transcribing = False
    c.current_backend.is_remote = False
    c.streaming_runtime.stop_streaming_session.return_value = ""
    h.ui.clear_transcription_stats = lambda: None
    h.settings.save_setting(SettingsKey.ACTIONS, [CHANNEL.to_dict()])
    h.saves = []
    c.persistence_executor = SimpleNamespace(submit=lambda fn, *args: h.saves.append((fn, args)))
    return h


def live_state(h):
    """QtAppControl.state() for the harness's controller."""
    return QtAppControl.state(SimpleNamespace(_require=lambda: h.controller))


def run_worker(h):
    while h.submitted:
        fn, args = h.submitted.pop(0)
        fn(*args)


def test_the_live_state_shows_a_routed_dictation_as_its_actions_job(app, monkeypatch):
    app.controller.current_backend.transcribe = lambda path: "claude, what time is it"
    words.route_when_claude_is_named(monkeypatch)
    assert app.runtime.start_recording(source="hotkey", route="hotkey")
    (started,) = app.bus.recent(types=["recording.started"])
    job_id = started.data["job_id"]

    recording = live_state(app)
    assert (recording.recording, recording.busy, recording.mode, recording.action_id,
            recording.job_id) == (True, False, "dictation", "", job_id)

    app.runtime.stop_recording()
    run_worker(app)  # transcribed, routed to the channel action and delivered
    routed = app.controller.action_runtime.deliver.call_args[0][0]
    assert routed.action_id == "ch" and app.runtime.has_active_job

    saving = live_state(app)
    assert (saving.recording, saving.busy, saving.mode, saving.action_id, saving.job_id) == (
        False, True, "action", "ch", job_id)
