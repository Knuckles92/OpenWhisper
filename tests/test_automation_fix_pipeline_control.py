"""Cancel reaches filters, routing and actions; what a program may run; route failures."""

import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import focus_context
from services.actions import routing
from services.actions.model import Action, ActionKind
from services.automation import filters, permissions
from services.automation.control import ControlError
from services.automation.filters import FilterOutcome
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import FocusSnapshot
from services.settings import SettingsKey
from tests import test_automation_foundation_pipeline as foundation

# The foundation's harness fixtures, used here by name.
_qapp = foundation._qapp
h = foundation.h
_events = foundation._events
ASK, RAW = foundation.ASK, foundation.RAW

AGENT = Action("ag", "Ask my agent", ActionKind.AGENT,
               {"agent": "claude_code", "mode": "ask", "include_selection": True})
CHANNEL = Action("ch", "Tell my session", ActionKind.CHANNEL, {})


def _with_actions(h, *actions, **values):
    h.settings.save_setting(SettingsKey.ACTIONS, [a.to_dict() for a in (ASK, RAW, *actions)])
    for key, value in values.items():
        h.settings.save_setting(key, value)


# -- Cancel stops filters and routing (concurrency #3) ---------------------------

def _fake_filters(monkeypatch, apply):
    """``apply`` stands in for filters.apply and both parts of a StageRun,
    which a dictation that may be routed runs around routing."""

    class StageRun:
        def __init__(self, stage, settings=None, *, cancel=None):
            self.stage, self.settings, self.cancel = stage, settings, cancel

        def saved(self, text, job):
            return apply(f"{self.stage} saved", text, job, self.settings, cancel=self.cancel)

        def leased(self, text, job):
            return apply(f"{self.stage} leased", text, job, self.settings, cancel=self.cancel)

    monkeypatch.setattr(filters, "apply", apply)
    monkeypatch.setattr(filters, "StageRun", StageRun)


def test_filters_and_routing_get_the_cancel(h, monkeypatch):
    seen = []

    def apply(stage, text, job, settings=None, *, cancel=None):
        seen.append((stage, cancel))
        return FilterOutcome(text)

    def decide(text, job, settings, *, judge=None, cancel=None):
        seen.append(("route", cancel))
        return None

    _fake_filters(monkeypatch, apply)
    monkeypatch.setattr(routing, "decide", decide)
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION, route="hotkey"))
    h.runtime.transcribe_audio_file(h.audio)

    assert [stage for stage, _ in seen] == [
        "before_cleanup saved", "route", "before_cleanup leased", "after_cleanup"]
    assert all(cancel is h.runtime._cancel_requested for _, cancel in seen)


@pytest.mark.parametrize(
    "cancel_at", ["before_cleanup saved", "route", "before_cleanup leased"])
def test_canceled_words_reach_no_later_stage(h, monkeypatch, cancel_at):
    reached = []

    def apply(stage, text, job, settings=None, *, cancel=None):
        reached.append(stage)
        if stage == cancel_at:
            h.runtime.cancel()  # Cancel pressed while this stage ran
        return FilterOutcome(text)

    def decide(text, job, settings, *, judge=None, cancel=None):
        reached.append("route")
        if cancel_at == "route":
            h.runtime.cancel()
        return None

    h.controller.current_backend.is_transcribing = False
    _fake_filters(monkeypatch, apply)
    monkeypatch.setattr(routing, "decide", decide)
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION, route="hotkey"))
    h.runtime.transcribe_audio_file(h.audio)

    assert reached[-1] == cancel_at
    assert h.runtime._transcript_cleanup.calls == []
    assert h.ui.stages == [] and h.history.entries == []
    assert not h.runtime.has_active_job
    assert _events(h, "transcription.failed")


@pytest.mark.parametrize("canceled_a_job", [True, False])
def test_cancel_stops_running_actions_only_with_no_job_to_cancel(canceled_a_job):
    from services.application_controller import ApplicationController

    controller = SimpleNamespace(transcription_runtime=Mock(), actions_cancel_requested=Mock())
    controller.transcription_runtime.cancel.return_value = canceled_a_job
    ApplicationController.cancel(controller)
    controller.transcription_runtime.cancel.assert_called_once_with()
    assert controller.actions_cancel_requested.emit.call_count == (0 if canceled_a_job else 1)


# -- what a program may run (security #7, #8, concurrency #6) --------------------

@pytest.mark.parametrize("action", [AGENT, CHANNEL])
@pytest.mark.parametrize("source", ["api", "mcp"])
def test_a_program_needs_the_agent_permission_for_agents_and_channels(h, action, source):
    _with_actions(h, action)
    with pytest.raises(permissions.PermissionDenied) as denied:
        h.runtime.run_action_text(action.id, "rm -rf my project", source=source)
    assert denied.value.permission == permissions.AGENT_PROMPTS
    assert not h.runtime.has_active_job

    h.settings.save_setting(SettingsKey.API_AGENT_PROMPTS, True)
    assert h.runtime.run_action_text(action.id, "hello", source=source)
    assert h.runtime.has_active_job


def test_other_actions_need_no_agent_permission(h):
    assert h.runtime.run_action_text("a1", "hello", source="api")


def test_no_new_run_while_actions_refuse_work(h):
    h.controller.action_runtime.accepting = lambda: False
    with pytest.raises(ControlError) as busy:
        h.runtime.run_action_text("a1", "hello", source="api")
    assert busy.value.code == "busy"
    assert not h.runtime.has_active_job


class _RecordingService(foundation.FakeService):
    def __init__(self):
        self.selections = []

    def request(self, *, include_text, include_selection=False):
        self.selections.append(include_selection)
        future: Future = Future()
        future.set_result(FocusSnapshot())
        return future


@pytest.fixture
def service():
    recording = _RecordingService()
    focus_context.set_service(recording)
    return recording


@pytest.mark.parametrize("source", ["api", "mcp"])
def test_a_program_run_never_reads_the_selection(h, service, source):
    _with_actions(h, AGENT, **{SettingsKey.APP_CONTEXT_ENABLED: True,
                               SettingsKey.API_AGENT_PROMPTS: True})
    h.runtime.run_action_text("ag", "hello", source=source)
    assert service.selections == [False]


class _Recorder:
    is_recording = False
    capture_canceled = False

    def start_recording(self):
        self.is_recording = True
        return True


def _recordable(h):
    h.controller.recorder = _Recorder()
    h.controller.recording_state_changed = foundation.Signal()
    h.ui.clear_transcription_stats = lambda: None


@pytest.mark.parametrize("source, reads", [("api", False), ("mcp", False), ("hotkey", True),
                                           ("ui", True)])
def test_a_recording_reads_the_selection_only_when_the_user_started_it(h, service, source, reads):
    _with_actions(h, AGENT, **{SettingsKey.APP_CONTEXT_ENABLED: True})
    _recordable(h)
    assert h.runtime.start_recording(mode=JobMode.ACTION, action_id="ag", source=source)
    assert service.selections == [reads]


class _StoppingRecorder:
    """A stop that takes a moment after the stream has stopped."""

    capture_canceled = False

    def __init__(self):
        self.is_recording = True
        self.stopped, self.proceed = threading.Event(), threading.Event()

    def stop_recording(self):
        self.is_recording = False
        self.stopped.set()
        self.proceed.wait(2)
        return True

    def get_recording_duration(self):
        return 1.0


def test_a_program_run_cannot_take_the_slot_from_a_recording_being_stopped(h):
    recorder = h.controller.recorder = _StoppingRecorder()
    h.controller._streaming_enabled = False
    h.controller.recording_state_changed = foundation.Signal()
    h.runtime._job = DictationJob(mode=JobMode.DICTATION, source="hotkey")
    stopper = threading.Thread(target=h.runtime.stop_recording)
    stopper.start()
    assert recorder.stopped.wait(2)

    outcome = []

    def run():
        try:
            outcome.append(h.runtime.run_action_text("a1", "hello", source="api"))
        except ControlError as exc:
            outcome.append(exc.code)

    runner = threading.Thread(target=run)
    runner.start()
    time.sleep(0.15)
    finished_early = bool(outcome)
    recorder.proceed.set()
    stopper.join(2)
    runner.join(2)

    # Refused at once (the stop holds the capture lock), never waiting for it.
    assert finished_early
    assert outcome == ["busy"]
    assert [fn.__name__ for fn, _args in h.submitted] == ["finish_recording_job"]


# -- Route by voice says when the judge couldn't answer (behaviour #7) -----------

@pytest.mark.parametrize("route, notices", [
    ("hotkey", ["Couldn't reach Fast judgments — pasted instead"]),
    # Any dictation may be routed; it stays as quiet as a plain paste.
    ("auto", []),
])
@pytest.mark.parametrize("failure", ["timeout", "unreachable"])
def test_a_judge_failure_pastes_and_route_by_voice_says_so(h, monkeypatch, route, notices,
                                                           failure):
    monkeypatch.setattr(routing, "decide", lambda *a, **k: routing.RouteDecision(
        "", "", 0.0, failure=failure))
    h.controller._pending_audio_path = h.audio  # a live recording
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION, route=route))
    h.runtime.transcribe_audio_file(h.audio)

    h.paste.assert_called_once_with()
    h.controller.action_runtime.deliver.assert_not_called()
    assert h.ui.notices == notices
    entry, = h.history.entries
    assert entry["entry_kind"] == "dictation"


# -- an empty transcript is not a filter's cancel (pipeline #10A) -----------------

def _keeping_filter(monkeypatch, replies=None):
    def apply(stage, text, job, settings=None, *, cancel=None):
        return FilterOutcome((replies or {}).get(stage, text))

    monkeypatch.setattr(filters, "apply", apply)


def test_an_empty_transcript_is_not_taken_for_a_filter_cancel(h, monkeypatch):
    _keeping_filter(monkeypatch)
    job = DictationJob(mode=JobMode.DICTATION)
    assert h.runtime._claim_job(job)
    assert h.runtime._run_filters("before_cleanup", "   ", job, {}) == (job, "   ")
    assert not h.runtime._filter_canceled


def test_a_filter_that_empties_the_words_or_cancels_takes_delivery(h, monkeypatch):
    _keeping_filter(monkeypatch, {"before_cleanup": "  "})
    job = DictationJob(mode=JobMode.DICTATION)
    assert h.runtime._claim_job(job)
    assert h.runtime._run_filters("before_cleanup", "hello", job, {}) == (job, None)
    assert h.runtime._filter_canceled

    h.runtime._finish_job()
    monkeypatch.setattr(filters, "apply", lambda stage, text, job, settings=None, **k:
                        FilterOutcome(text, canceled=True))
    assert h.runtime._claim_job(job)
    assert h.runtime._run_filters("before_cleanup", "hello", job, {}) == (job, None)
    assert h.runtime._filter_canceled


def test_an_empty_dictation_with_filters_reports_no_speech(h, monkeypatch):
    _keeping_filter(monkeypatch)
    h.controller.current_backend.transcribe = lambda path: ""
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))
    h.runtime.transcribe_audio_file(h.audio)

    assert h.ui.statuses[-1] == "No speech detected (empty after VAD)"
    failed, = _events(h, "transcription.failed")
    assert failed.data["error"] == "no_speech"


def test_a_real_no_match_still_says_no_action_matched(h, monkeypatch):
    monkeypatch.setattr(routing, "decide", lambda *a, **k: None)
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION, route="hotkey"))
    h.runtime.transcribe_audio_file(h.audio)
    assert h.ui.notices == ["No action matched — pasted instead"]

    # The next job starts without the earlier one's failure.
    h.ui.notices.clear()
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION, route="auto"))
    h.runtime.transcribe_audio_file(h.audio)
    assert h.ui.notices == []
