"""An action's words survive shaping problems, and events never carry a selection.

Review findings: a transform-shaped action whose model refused or failed, an
action deleted while recording, and a routed dictation into either, used to
take Command Mode's refusal path, which dropped the audio and saved nothing.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import config
from services import text_rewrite
from services.actions import routing
from services.actions.model import Action, ActionKind
from services.cleanup_profiles import CleanupProfile
from services.dictation_pipeline import DictationJob, JobMode
from services.runtime.command import CommandRefused
from services.settings import SettingsKey
from tests import test_automation_foundation_pipeline as foundation

# The foundation's harness fixtures, used here by name.
_qapp = foundation._qapp
h = foundation.h
_events = foundation._events
ASK, RAW = foundation.ASK, foundation.RAW

POLISHED = Action("a3", "Prompt Claude", ActionKind.SCRIPTS, {}, shaping="transform:polish")
EMAIL = CleanupProfile("email", "Email", "Write it as an email.")
FORMAL = Action("a4", "Formal note", ActionKind.SCRIPTS, {}, shaping="profile:email")


def _live_recording(h):
    """The job's audio is the recorder's own WAV, as for a real recording."""
    acknowledge = Mock()
    h.controller.recorder = SimpleNamespace(is_recording=False, acknowledge_recording=acknowledge)
    h.controller._pending_audio_path = config.RECORDED_AUDIO_FILE
    preserved = []
    h.runtime._preserve_failed_audio = lambda path, status: preserved.append(path)
    return acknowledge, preserved


def _with_actions(h, *actions):
    h.settings.save_setting(SettingsKey.ACTIONS, [a.to_dict() for a in (ASK, RAW, *actions)])


def _delivered(h):
    (args, kwargs), = h.controller.action_runtime.deliver.call_args_list
    return args[0], args[1], kwargs


def _never_mentions_command_mode(h):
    shown = [str(c) for c in h.controller.status_update.calls] + h.ui.statuses + h.ui.notices
    assert not any("Command Mode" in text for text in shown)


def test_a_transform_without_an_ai_provider_sends_the_cleaned_words(h):
    _with_actions(h, POLISHED)
    h.runtime._transcript_cleanup.is_available = lambda: False
    acknowledge, preserved = _live_recording(h)
    assert h.runtime._claim_job(DictationJob(mode=JobMode.ACTION, action_id="a3"))
    h.runtime.transcribe_audio_file(h.audio)

    job, text, kwargs = _delivered(h)
    assert job.action_id == "a3" and text == "ask claude what time it is"
    assert kwargs["notice"] == "Polish couldn't run — sent your cleaned words"
    entry, = h.history.entries
    assert entry["text"] == "ask claude what time it is" and entry["entry_kind"] == "action"
    acknowledge.assert_not_called()
    assert preserved == [] and _events(h, "transcription.failed", timeout=0.05) == []
    assert not h.runtime.has_active_job
    _never_mentions_command_mode(h)


@pytest.mark.parametrize("error", [
    "The AI model took too long. Try a faster one in AI cleanup",  # a failure
    text_rewrite.TOO_LONG_MESSAGE,                                  # a refusal
])
def test_a_transform_the_model_fails_or_refuses_sends_the_cleaned_words(h, monkeypatch, error):
    _with_actions(h, POLISHED)
    monkeypatch.setattr(text_rewrite, "rewrite_with", lambda *a, **k: ("", error))
    acknowledge, _preserved = _live_recording(h)
    assert h.runtime._claim_job(DictationJob(mode=JobMode.ACTION, action_id="a3"))
    h.runtime.transcribe_audio_file(h.audio)

    _job, text, kwargs = _delivered(h)
    assert text == "ASK CLAUDE WHAT TIME IT IS"  # cleanup ran; the transform didn't
    assert kwargs["notice"] == "Polish couldn't run — sent your cleaned words"
    assert len(h.history.entries) == 1
    acknowledge.assert_not_called()


def test_an_action_deleted_while_recording_still_gets_the_words_and_history(h):
    acknowledge, preserved = _live_recording(h)
    job = DictationJob(mode=JobMode.ACTION, action_id="deleted-meanwhile", source="hotkey")
    assert h.runtime._claim_job(job)
    h.runtime.transcribe_audio_file(h.audio)

    # The action runtime says it no longer exists and copies the words.
    delivered, text, _kwargs = _delivered(h)
    assert delivered is job and text == "ASK CLAUDE WHAT TIME IT IS"
    entry, = h.history.entries
    assert entry["text"] == "ASK CLAUDE WHAT TIME IT IS"
    acknowledge.assert_not_called()
    assert preserved == []
    assert not h.runtime.has_active_job


def test_a_routed_dictation_into_a_refusing_transform_is_not_lost(h, monkeypatch):
    _with_actions(h, POLISHED)

    def refuse(text, instruction):
        raise CommandRefused(text_rewrite.NO_PROVIDER_MESSAGE)

    monkeypatch.setattr(h.controller.command_runtime, "rewrite_text", refuse)
    monkeypatch.setattr(routing, "decide",
                        lambda *a, **k: routing.RouteDecision("a3", "what time it is", 0.9))
    acknowledge, _preserved = _live_recording(h)
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION, route="auto"))
    h.runtime.transcribe_audio_file(h.audio)

    job, text, kwargs = _delivered(h)
    assert job.routed and text == "WHAT TIME IT IS"
    assert kwargs["notice"] == "Polish couldn't run — sent your cleaned words"
    assert len(h.history.entries) == 1
    acknowledge.assert_not_called()


def test_a_cancel_during_the_transform_is_still_a_cancel(h, monkeypatch):
    _with_actions(h, POLISHED)

    def canceled(text, instruction):
        h.runtime._cancel_requested.set()
        raise RuntimeError("Transcription canceled")

    monkeypatch.setattr(h.controller.command_runtime, "rewrite_text", canceled)
    assert h.runtime._claim_job(DictationJob(mode=JobMode.ACTION, action_id="a3"))
    h.runtime.transcribe_audio_file(h.audio)

    h.controller.action_runtime.deliver.assert_not_called()
    assert h.history.entries == []


@pytest.mark.parametrize("mode", [JobMode.ACTION, JobMode.DICTATION])
def test_a_refusal_never_drops_a_dictation_or_action_recording(h, mode):
    acknowledge, preserved = _live_recording(h)
    assert h.runtime._claim_job(DictationJob(mode=mode, action_id="a1"))
    h.runtime._refuse("Nothing to do")

    acknowledge.assert_not_called()
    assert preserved == [config.RECORDED_AUDIO_FILE]


def test_a_command_refusal_still_drops_its_instruction_audio(h):
    acknowledge, preserved = _live_recording(h)
    assert h.runtime._claim_job(DictationJob(mode=JobMode.COMMAND))
    h.runtime._refuse("Select text first")

    acknowledge.assert_called_once_with()
    assert preserved == []


# -- an action's formatting failure is shown (finding 13) --------------------------

class _FailingCleaner:
    def __init__(self):
        self.last_error = None
        self.provider, self.model = "openrouter", "test/model"

    def configure(self, provider, model, reasoning=None):
        pass

    def is_available(self):
        return True

    def cleanup(self, text, system_prompt=None, timeout_s=None, deadline_s=None):
        self.last_error = "timeout"
        return text


def test_an_api_run_profile_action_says_its_formatting_failed(h):
    _with_actions(h, FORMAL)
    h.settings.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_PROFILES, [EMAIL.to_dict()])
    h.runtime._transcript_cleanup = _FailingCleaner()
    h.runtime.run_action_text("a4", "hello there", source="api")
    fn, args = h.submitted.pop(0)
    fn(*args)

    _job, text, kwargs = _delivered(h)
    assert text == "hello there"
    assert kwargs["notice"] == "Email formatting failed (timeout); using raw transcript"


def test_an_action_without_problems_has_no_notice(h):
    assert h.runtime._claim_job(DictationJob(mode=JobMode.ACTION, action_id="a1"))
    h.runtime.transcribe_audio_file(h.audio)
    _job, _text, kwargs = _delivered(h)
    assert kwargs["notice"] == ""


def test_a_profile_dictation_still_shows_the_failure_in_its_status(h):
    h.settings.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_PROFILES, [EMAIL.to_dict()])
    h.runtime._transcript_cleanup = _FailingCleaner()
    h.runtime._recording_profile = EMAIL
    h.runtime._profile_settings = h.settings.load_all_settings()
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))
    h.runtime._recording_profile = EMAIL
    h.runtime._profile_settings = h.settings.load_all_settings()
    h.runtime.transcribe_audio_file(h.audio)

    assert h.ui.statuses[-1] == (
        "Ready (Pasted) — Email formatting failed (timeout); using raw transcript"
    )


# -- events never carry the selection (finding 2) ---------------------------------

SECRET = "Dear Sam, my bank PIN is 4512 and the door code is 0091."


@pytest.mark.parametrize("mode", [JobMode.TRANSFORM, JobMode.COMMAND])
def test_a_rewrite_publishes_no_selected_text(h, mode):
    job = DictationJob(mode=mode, selection=SECRET)
    assert h.runtime.begin_rewrite_job(job, source_name="Transform · Polish")
    # CommandRuntime._rewrite returns the selection as raw_text.
    h.runtime.submit_rewrite(lambda: ("Dear Sam, here are the codes.", SECRET, None))
    fn, args = h.submitted.pop(0)
    fn(*args)

    completed, = _events(h, "transcription.completed")
    assert completed.data["raw_text"] is None
    assert SECRET not in str(completed.data)
    # History keeps the original, as before.
    assert h.history.entries[0]["raw_text"] == SECRET


def test_dictations_actions_and_files_keep_their_before_cleanup_text(h):
    from services.automation import catalog

    for mode in (JobMode.DICTATION, JobMode.ACTION):
        data = catalog.transcription_completed(
            DictationJob(mode=mode), text="Hello.", raw_text="hello")
        assert data["raw_text"] == "hello"
    data = catalog.transcription_completed(None, text="Hello.", raw_text="hello")
    assert data["mode"] == "file" and data["raw_text"] == "hello"


def test_the_action_runtime_shows_the_notice_with_its_outcome(h, monkeypatch):
    from services.actions import runtime as action_runtime

    from services.automation import events

    _with_actions(h, POLISHED)
    monkeypatch.setattr(action_runtime, "settings_manager", h.settings)
    runtime = action_runtime.ActionRuntime(h.controller)
    # A program following the live events, so "Send to my scripts" has a listener.
    listener = events.bus().subscribe(lambda event: None, label="sse")
    try:
        runtime.deliver(DictationJob(mode=JobMode.ACTION, action_id="a3", source="hotkey"), "hello",
                        raw_text=None, html="",
                        notice="Polish couldn't run — sent your cleaned words")
        runtime.deliver(DictationJob(mode=JobMode.ACTION, action_id="gone", source="hotkey"), "hello",
                        raw_text=None, html="")
    finally:
        runtime.close()
        listener.close()
    assert h.ui.statuses == [
        "Sent to your scripts — Polish couldn't run — sent your cleaned words",
        "That action no longer exists — copied instead",
    ]
    assert h.ui.copied == ["hello"]
