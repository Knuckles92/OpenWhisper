"""Actions, text filters, routing and events through the real transcription runtime."""

import os
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services import focus_context
from services.actions import routing
from services.actions.model import Action, ActionKind
from services.automation import events, filters
from services.automation.control import ControlError
from services.automation.events import EventBus
from services.automation.filters import FilterOutcome
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import ContextCaptureService, FocusSnapshot
from services.runtime import command, transcription
from services.runtime.command import CommandRuntime
from services.runtime.transcription import FILTER_CANCELED_MESSAGE, TranscriptionRuntime
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings

ASK = Action("a1", "Ask Claude", ActionKind.OPEN_LINK,
             {"template": "claude://claude.ai/new?q={text}"}, spoken_names=("Claude",))
RAW = Action("a2", "Raw notes", ActionKind.SCRIPTS, {}, shaping="raw")


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


class Signal:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def emit(self, *args):
        self.calls.append(args)
        if self.handler is not None:
            self.handler(*args)


class FakeUI:
    def __init__(self):
        self.statuses, self.stages, self.copied, self.notices = [], [], [], []
        self.discarded = 0
        self.main_window = SimpleNamespace(clear_partial_transcription=lambda: None)

    def set_transcript(self, text, raw=None):
        pass

    def show_transcription_error(self, message):
        pass

    def set_transcription_stats(self, *args, **kwargs):
        pass

    def set_status(self, text):
        self.statuses.append(text)

    def refresh_history(self):
        pass

    def discard_clipboard_prefetch(self):
        self.discarded += 1

    def copy_to_clipboard(self, text):
        self.copied.append(text)
        return True

    def show_notice(self, text, done=False):
        self.notices.append(text)

    def stage_transcript_for_paste(self, text, **kwargs):
        self.stages.append(text)
        return SimpleNamespace(written=True, lease=None, restore_unavailable=False)

    def schedule_clipboard_restore(self, stage):
        return True

    def insert_into_scratchpad(self, text):
        return False


class FakeHistory:
    def __init__(self):
        self.entries = []

    def add_entry(self, **fields):
        self.entries.append(fields)
        return SimpleNamespace(id=41 + len(self.entries), audio_file=None)


class FakeService(ContextCaptureService):
    def request(self, *, include_text, include_selection=False):
        future: Future = Future()
        future.set_result(FocusSnapshot())
        return future

    def current_identity(self):
        return None

    def reread(self, identity, callback):
        callback(None)

    def shutdown(self):
        pass


class Cleaner:
    """Uppercases, so a test can see whether cleanup ran."""

    def __init__(self):
        self.calls = []
        self.last_error = "not run"
        self.provider, self.model = "openrouter", "test/model"

    def configure(self, provider, model, reasoning=None):
        pass

    def is_available(self):
        return True

    def cleanup(self, text, system_prompt=None, timeout_s=None, deadline_s=None):
        self.calls.append(text)
        self.last_error = None
        return text.upper()


@pytest.fixture
def h(monkeypatch, tmp_path):
    bus = EventBus()
    previous_bus = events.set_bus(bus)
    seen = []
    bus.subscribe(seen.append)
    ui = FakeUI()
    history = FakeHistory()
    settings = InMemorySettings({
        SettingsKey.AUTO_PASTE: True,
        SettingsKey.RESTORE_CLIPBOARD: False,
        SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True,
        SettingsKey.ACTIONS: [ASK.to_dict(), RAW.to_dict()],
    })
    paste = Mock()
    for module in (transcription, command):
        monkeypatch.setattr(module, "settings_manager", settings)
    monkeypatch.setattr(transcription, "history_manager", history)
    monkeypatch.setattr(transcription, "send_paste", paste)
    monkeypatch.setattr(transcription, "is_accessibility_trusted", lambda: True)
    focus_context.set_service(FakeService())
    submitted = []
    controller = SimpleNamespace(
        ui_controller=ui,
        recorder=SimpleNamespace(is_recording=False),
        streaming_runtime=Mock(),
        is_meeting_active=lambda: False,
        overlay_state_update=Signal(),
        status_update=Signal(),
        transcription_completed=Signal(),
        transcription_failed=Signal(),
        executor=SimpleNamespace(submit=lambda fn, *args: submitted.append((fn, args))),
        persistence_executor=SimpleNamespace(submit=lambda fn, *args: fn(*args)),
        current_backend=SimpleNamespace(transcribe=lambda path: "ask claude what time it is",
                                        is_available=lambda: True),
        action_runtime=Mock(),
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
    controller.transcription_runtime = runtime
    controller.history_persisted = Signal(runtime.on_history_persisted)
    controller.transcription_completed.handler = runtime.on_transcription_complete
    controller.transcription_failed.handler = runtime.on_transcription_error
    runtime._incremental = Mock()
    runtime._incremental.transcribe.side_effect = lambda backend, path: backend.transcribe(path)
    runtime._transcript_cleanup = Cleaner()
    monkeypatch.setattr(runtime, "_model_info_for_history", lambda: "parakeet")
    controller.command_runtime = CommandRuntime(controller)
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFF" + bytes(200))
    yield SimpleNamespace(
        ui=ui, history=history, settings=settings, paste=paste, submitted=submitted,
        controller=controller, runtime=runtime, audio=str(audio), bus=bus, seen=seen,
    )
    controller.command_runtime.cleanup()
    bus.close()
    events.set_bus(previous_bus)


def _events(h, type_, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [e for e in h.seen if e.type == type_]
        if found:
            return found
        time.sleep(0.002)
    return [e for e in h.seen if e.type == type_]


def test_a_dictation_pastes_and_publishes_completion_and_history(h):
    job = DictationJob(mode=JobMode.DICTATION, source="hotkey")
    h.controller._pending_audio_path = h.audio  # a live recording
    assert h.runtime._claim_job(job)
    h.runtime.transcribe_audio_file(h.audio)

    assert h.ui.stages == ["ASK CLAUDE WHAT TIME IT IS"]
    h.paste.assert_called_once_with()
    h.controller.action_runtime.deliver.assert_not_called()
    entry, = h.history.entries
    assert "job_id" not in entry and entry["entry_kind"] == "dictation"
    completed, = _events(h, "transcription.completed")
    assert completed.data["job_id"] == job.id
    assert completed.data["text"] == "ASK CLAUDE WHAT TIME IT IS"
    assert completed.data["raw_text"] == "ask claude what time it is"
    assert completed.data["mode"] == "dictation" and completed.data["routed"] is False
    saved, = _events(h, "history.saved")
    assert saved.data == {"job_id": job.id, "record_id": "42", "kind": "dictation"}


def test_an_action_job_goes_to_the_action_not_the_cursor(h):
    h.controller._pending_source_name = "Action · Ask Claude"
    job = DictationJob(mode=JobMode.ACTION, action_id="a1", source="hotkey")
    assert h.runtime._claim_job(job)
    h.runtime.transcribe_audio_file(h.audio)

    h.paste.assert_not_called()
    assert h.ui.stages == []
    (args, kwargs), = h.controller.action_runtime.deliver.call_args_list
    assert args[0] is job and args[1] == "ASK CLAUDE WHAT TIME IT IS"
    assert kwargs["raw_text"] == "ask claude what time it is"
    entry, = h.history.entries
    assert entry["entry_kind"] == "action" and entry["source_name"] == "Action · Ask Claude"
    assert _events(h, "transcription.completed")[0].data["action_id"] == "a1"


def test_raw_shaping_skips_ai_cleanup(h):
    job = DictationJob(mode=JobMode.ACTION, action_id="a2")
    assert h.runtime._claim_job(job)
    h.runtime.transcribe_audio_file(h.audio)

    assert h.runtime._transcript_cleanup.calls == []
    assert h.controller.action_runtime.deliver.call_args[0][1] == "ask claude what time it is"


def test_a_filter_can_take_over_delivery_without_losing_the_words(h, monkeypatch):
    monkeypatch.setattr(filters, "apply",
                        lambda stage, text, job, settings=None: FilterOutcome(text, canceled=True))
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))
    h.runtime.transcribe_audio_file(h.audio)

    h.paste.assert_not_called()
    assert h.ui.statuses[-1] == FILTER_CANCELED_MESSAGE
    entry, = h.history.entries
    assert entry["text"] == "ask claude what time it is"
    assert not h.runtime.has_active_job
    # The next job delivers normally.
    monkeypatch.setattr(filters, "apply",
                        lambda stage, text, job, settings=None: FilterOutcome(text))
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))
    h.runtime.transcribe_audio_file(h.audio)
    h.paste.assert_called_once_with()


def test_an_after_cleanup_filter_replaces_the_final_text(h, monkeypatch):
    def apply(stage, text, job, settings=None):
        if stage == "after_cleanup":
            return FilterOutcome(text.replace("CLAUDE", "Claude"), changed=True)
        return FilterOutcome(text)

    monkeypatch.setattr(filters, "apply", apply)
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))
    h.runtime.transcribe_audio_file(h.audio)

    assert h.ui.stages == ["ASK Claude WHAT TIME IT IS"]
    entry, = h.history.entries
    assert entry["raw_text"] == "ask claude what time it is"


def test_a_failing_filter_keeps_the_text(h, monkeypatch):
    def apply(stage, text, job, settings=None):
        raise RuntimeError("bug in a filter")

    monkeypatch.setattr(filters, "apply", apply)
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))
    h.runtime.transcribe_audio_file(h.audio)
    assert h.ui.stages == ["ASK CLAUDE WHAT TIME IT IS"]


def test_routing_turns_a_dictation_into_an_action_job(h, monkeypatch):
    asked = []

    def decide(text, job, settings, *, judge=None):
        asked.append((text, job.route))
        return routing.RouteDecision("a1", "what time it is", 0.93)

    monkeypatch.setattr(routing, "decide", decide)
    job = DictationJob(mode=JobMode.DICTATION, route="hotkey")
    assert h.runtime._claim_job(job)
    h.runtime.transcribe_audio_file(h.audio)

    assert asked == [("ask claude what time it is", "hotkey")]
    h.paste.assert_not_called()
    routed, text = h.controller.action_runtime.deliver.call_args[0]
    assert routed.mode == JobMode.ACTION and routed.action_id == "a1" and routed.routed
    assert routed.id == job.id and text == "WHAT TIME IT IS"
    entry, = h.history.entries
    assert entry["entry_kind"] == "action" and entry["source_name"] == "Action · Ask Claude"
    assert _events(h, "transcription.completed")[0].data["routed"] is True


def test_no_route_or_an_unknown_action_pastes_as_usual(h, monkeypatch):
    monkeypatch.setattr(routing, "decide", lambda *a, **k: routing.RouteDecision("gone", "x", 1))
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION, route="hotkey"))
    h.runtime.transcribe_audio_file(h.audio)
    h.paste.assert_called_once_with()

    calls = []
    monkeypatch.setattr(routing, "decide", lambda *a, **k: calls.append(1))
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))
    h.runtime.transcribe_audio_file(h.audio)
    assert calls == []  # route "" never asks


def test_run_action_text_runs_without_recording(h):
    job_id = h.runtime.run_action_text("a1", "  hello there ", source="api")
    assert h.runtime.has_active_job
    fn, args = h.submitted.pop(0)
    fn(*args)

    job, text = h.controller.action_runtime.deliver.call_args[0]
    assert job.id == job_id and job.source == "api" and text == "HELLO THERE"
    entry, = h.history.entries
    assert entry["source_name"] == "Action · Ask Claude" and entry["entry_kind"] == "action"
    assert not h.runtime.has_active_job


def test_run_action_text_refusals(h):
    with pytest.raises(ControlError) as refused:
        h.runtime.run_action_text("nope", "hi", source="api")
    assert refused.value.code == "not_found"
    with pytest.raises(ControlError) as empty:
        h.runtime.run_action_text("a1", "   ", source="api")
    assert empty.value.code == "refused"
    assert h.runtime._claim_job(DictationJob())
    with pytest.raises(ControlError) as busy:
        h.runtime.run_action_text("a1", "hi", source="api")
    assert busy.value.code == "busy"


def test_failures_are_published(h):
    job = DictationJob(mode=JobMode.DICTATION)
    assert h.runtime._claim_job(job)
    h.runtime.on_transcription_error("engine exploded")
    failed, = _events(h, "transcription.failed")
    assert failed.data == {"job_id": job.id, "error": "engine exploded"}


def test_a_dictation_that_may_be_routed_learns_its_app_without_app_awareness(monkeypatch):
    from services import dictation_pipeline
    from services.settings import SettingsKey as Key

    requests = []

    class Service(FakeService):
        def request(self, *, include_text, include_selection=False):
            requests.append(include_text)
            return super().request(include_text=include_text)

    focus_context.set_service(Service())
    off = {Key.APP_CONTEXT_ENABLED: False}
    plain = dictation_pipeline.begin_job(JobMode.DICTATION, off)
    assert plain.target is None and requests == []
    routed = dictation_pipeline.begin_job(JobMode.DICTATION, off, route="hotkey")
    assert routed.target is not None and routed.focus is None and requests == [False]
