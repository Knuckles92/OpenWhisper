"""Review round 3, F1: a Cancel after a job's result was delivered, while it is saved.

The real TranscriptionRuntime and ActionRuntime and the controller's Cancel;
history saving is held, as a slow WAV copy, fsync or SQLite write holds it,
so the job slot is still claimed while the action it ran is working.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.actions import agents
from services.actions import runtime as actions_runtime
from services.actions.model import Action, ActionKind
from services.actions.runtime import ActionRuntime
from services.automation import app_control, catalog
from services.automation.control import ControlError
from services.dictation_pipeline import DictationJob, JobMode
from services.settings import SettingsKey
from tests import test_automation_foundation_pipeline as foundation
from tests.test_automation_actions_runtime import _wait
from tests.test_automation_fix_actions_runtime import BlockedAsk

_qapp = foundation._qapp
h = foundation.h

QUICK = Action("q1", "Quick answer", ActionKind.AGENT,
               {"agent": "claude_code", "mode": "ask", "output": "notice"})


@pytest.fixture
def pipe(h, monkeypatch):
    monkeypatch.setattr(actions_runtime, "settings_manager", h.settings)
    h.controller.action_runtime = ActionRuntime(h.controller)
    h.controller.current_backend.is_transcribing = False
    h.settings.save_setting(SettingsKey.ACTIONS, [QUICK.to_dict()])
    # History saves wait here until the test lets them run.
    h.saves = []
    h.controller.persistence_executor = SimpleNamespace(
        submit=lambda fn, *args: h.saves.append((fn, args)))
    h.ask = BlockedAsk()
    monkeypatch.setattr(agents, "ask", h.ask)
    yield h
    h.ask.release.set()
    h.controller.action_runtime.close()


def users_cancel(h):
    """ApplicationController.cancel, with its signal calling the slot at once."""
    from services.application_controller import ApplicationController

    controller = SimpleNamespace(
        transcription_runtime=h.runtime, action_runtime=h.controller.action_runtime)
    controller.actions_cancel_requested = SimpleNamespace(
        emit=lambda: ApplicationController._cancel_actions(controller))
    ApplicationController.cancel(controller)


def save_history(h):
    while h.saves:
        fn, args = h.saves.pop(0)
        fn(*args)


def types(h):
    """What was published so far, in order (the replay buffer is written at once)."""
    return [event.type for event in h.bus.recent(500)]


def quick_answer_delivered(h):
    """A Quick answer dictation, delivered and thinking, its history not saved yet."""
    h.controller._pending_audio_path = h.audio
    assert h.runtime._claim_job(DictationJob(mode=JobMode.ACTION, action_id="q1", source="hotkey"))
    h.runtime.transcribe_audio_file(h.audio)
    _wait(lambda: h.ask.started(1))
    assert h.runtime.has_active_job and h.saves


def test_the_users_cancel_while_a_delivered_job_is_saved_stops_its_quick_answer(pipe):
    h = pipe
    quick_answer_delivered(h)

    users_cancel(h)

    (_text, cancel), = h.ask.calls
    assert cancel.is_set()
    _wait(lambda: h.ui.statuses[-1] == "Canceled Quick answer")
    # Nothing was recording or transcribing, so no recording says it was canceled.
    assert catalog.RECORDING_CANCELED not in types(h)

    save_history(h)
    assert not h.runtime.has_active_job
    ran = foundation._events(h, catalog.ACTION_RAN)[-1].data
    assert (ran["ok"], ran["message"]) == (False, actions_runtime.CANCELED_MESSAGE)
    foundation._events(h, catalog.HISTORY_SAVED)
    assert catalog.RECORDING_CANCELED not in types(h)
    assert catalog.TRANSCRIPTION_FAILED not in types(h)
    # The words were saved in History all the same.
    assert [entry["entry_kind"] for entry in h.history.entries] == ["action"]


def test_the_runtime_has_nothing_to_cancel_once_the_result_is_delivered(pipe):
    h = pipe
    quick_answer_delivered(h)

    assert h.runtime.cancelable is False
    assert h.runtime.cancel() is False
    assert not h.runtime._cancel_requested.is_set()
    # A new recording still waits for the save.
    assert h.runtime.has_active_job
    (_text, cancel), = h.ask.calls
    assert not cancel.is_set()


def test_an_api_cancel_while_a_delivered_job_is_saved_is_refused(pipe):
    h = pipe
    quick_answer_delivered(h)
    statuses = list(h.ui.statuses)
    reports = list(h.controller.status_update.calls)
    overlays = list(h.controller.overlay_state_update.calls)
    controller = SimpleNamespace(recorder=h.controller.recorder,
                                 transcription_runtime=h.runtime)

    with pytest.raises(ControlError) as refused:
        app_control._cancel(controller)

    assert (refused.value.code, refused.value.message) == (
        "refused", "Nothing is recording or transcribing.")
    assert catalog.RECORDING_CANCELED not in types(h)
    # The user is told nothing: the API's answer is the report.
    assert h.ui.statuses == statuses
    assert h.controller.status_update.calls == reports
    assert h.controller.overlay_state_update.calls == overlays
    # The user's Quick answer goes on.
    (_text, cancel), = h.ask.calls
    assert not cancel.is_set()


def test_a_cancel_before_the_result_is_delivered_still_cancels_the_job(pipe):
    h = pipe
    h.controller._pending_audio_path = h.audio
    assert h.runtime._claim_job(DictationJob(mode=JobMode.ACTION, action_id="q1", source="hotkey"))
    # The worker finished, but its result hasn't reached the Qt thread yet.
    emitted = []
    h.controller.transcription_completed.handler = lambda *args: emitted.append(args)
    h.runtime.transcribe_audio_file(h.audio)

    assert h.runtime.cancelable is True
    assert h.runtime.cancel() is True
    h.runtime.on_transcription_complete(*emitted[0])

    assert h.ask.calls == []
    failed = foundation._events(h, catalog.TRANSCRIPTION_FAILED)[-1].data
    assert failed["error"] == "canceled"
    assert types(h).count(catalog.RECORDING_CANCELED) == 1


def test_a_failed_jobs_audio_being_kept_is_not_canceled(pipe):
    h = pipe
    h.controller._pending_audio_path = h.audio
    assert h.runtime._claim_job(DictationJob(mode=JobMode.ACTION, action_id="q1", source="hotkey"))
    h.runtime.on_transcription_error("engine exploded")
    assert h.runtime.has_active_job and h.saves  # keeping the audio

    assert h.runtime.cancel() is False
    assert catalog.RECORDING_CANCELED not in types(h)
    failed, = [e.data for e in h.bus.recent(types=[catalog.TRANSCRIPTION_FAILED])]
    assert failed["error"] == "failed"
