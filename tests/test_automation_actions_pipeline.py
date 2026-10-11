"""An action job from its words to its answer, through the real transcription runtime."""

import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QCoreApplication

from services import focus_context
from services.actions import runtime as action_runtime
from services.actions.model import Action, ActionKind
from services.actions.runtime import ActionRuntime
from services.automation import catalog, events, programs
from services.automation.events import EventBus
from services.focus_context import AppIdentity, ContextCaptureService, FocusSnapshot
from services.runtime import transcription
from services.runtime.transcription import TranscriptionRuntime
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings

EDITOR = AppIdentity("code.exe", "Code", pid=7, window="0x7")
FIX = Action("fix", "Fix grammar", ActionKind.PROGRAM,
             {"program": "C:\\fix.exe", "output": "paste"}, shaping="raw")


class Signal:
    def __init__(self, handler=None):
        self.handler = handler

    def emit(self, *args):
        if self.handler is not None:
            self.handler(*args)


class FakeUI:
    def __init__(self):
        self.stages, self.notices, self.statuses, self.copied = [], [], [], []
        self.scratchpad = []
        self.main_window = SimpleNamespace(clear_partial_transcription=lambda: None)

    def set_transcript(self, text, raw=None):
        pass

    def set_transcription_stats(self, *args, **kwargs):
        pass

    def show_transcription_error(self, message):
        pass

    def refresh_history(self):
        pass

    def set_status(self, text):
        self.statuses.append(text)

    def show_notice(self, text, *, done=False):
        self.notices.append(text)

    def discard_clipboard_prefetch(self):
        pass

    def copy_to_clipboard(self, text, html=""):
        self.copied.append(text)
        return True

    def stage_transcript_for_paste(self, text, **kwargs):
        self.stages.append(text)
        return SimpleNamespace(written=True, lease=None, restore_unavailable=False)

    def schedule_clipboard_restore(self, stage):
        return True

    def show_in_scratchpad(self, text):
        self.scratchpad.append(text)


class FakeService(ContextCaptureService):
    def request(self, *, include_text, include_selection=False):
        future: Future = Future()
        future.set_result(FocusSnapshot(identity=EDITOR))
        return future

    def current_identity(self):
        return EDITOR

    def reread(self, identity, callback):
        callback(None)

    def shutdown(self):
        pass


class FakeHistory:
    def __init__(self):
        self.entries = []

    def add_entry(self, **fields):
        self.entries.append(fields)
        return SimpleNamespace(id=len(self.entries), audio_file=None)


@pytest.fixture
def h(monkeypatch):
    bus = EventBus()
    previous_bus = events.set_bus(bus)
    settings = InMemorySettings({
        SettingsKey.AUTO_PASTE: False,
        SettingsKey.RESTORE_CLIPBOARD: False,
        SettingsKey.ACTIONS: [FIX.to_dict()],
    })
    for module in (transcription, action_runtime):
        monkeypatch.setattr(module, "settings_manager", settings)
    history = FakeHistory()
    paste = Mock()
    monkeypatch.setattr(transcription, "history_manager", history)
    monkeypatch.setattr(transcription, "send_paste", paste)
    monkeypatch.setattr(transcription, "is_accessibility_trusted", lambda: True)
    focus_context.set_service(FakeService())
    ui = FakeUI()
    jobs, saves = [], []
    controller = SimpleNamespace(
        ui_controller=ui,
        recorder=SimpleNamespace(is_recording=False),
        is_meeting_active=lambda: False,
        overlay_state_update=Signal(),
        status_update=Signal(),
        transcription_completed=Signal(),
        transcription_failed=Signal(),
        executor=SimpleNamespace(submit=lambda fn, *args: jobs.append((fn, args))),
        # History saves wait until the test runs them, like a slow disk.
        persistence_executor=SimpleNamespace(submit=lambda fn, *args: saves.append((fn, args))),
        current_backend=None,
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
    monkeypatch.setattr(runtime, "_model_info_for_history", lambda: "parakeet")
    controller.transcription_runtime = runtime
    controller.history_persisted = Signal(runtime.on_history_persisted)
    controller.transcription_completed.handler = runtime.on_transcription_complete
    controller.transcription_failed.handler = runtime.on_transcription_error
    controller.action_runtime = ActionRuntime(controller)
    yield SimpleNamespace(ui=ui, runtime=runtime, controller=controller, jobs=jobs,
                          saves=saves, history=history, paste=paste, bus=bus)
    controller.action_runtime.close()
    events.set_bus(previous_bus)
    bus.close()


def _run(queue):
    while queue:
        fn, args = queue.pop(0)
        fn(*args)


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out")


def test_a_quick_answer_pastes_while_the_job_still_saves_its_history(h, monkeypatch):
    seen = []

    def run_once(argv, stdin, *, timeout_s, cwd="", env=None, cancel=None):
        seen.append((stdin, threading.current_thread().name))
        return 0, b"Fixed my grammar.", ""

    monkeypatch.setattr(programs, "run_once", run_once)

    # The user's run: only theirs paste.
    job_id = h.runtime.run_action_text("fix", "fix my grammer", source="ui")
    _run(h.jobs)  # shaping on the transcription executor, then delivery

    _wait(lambda: h.bus.recent(types=[catalog.ACTION_RAN]))
    assert h.runtime.has_active_job and h.saves  # history not saved yet
    assert seen == [(b"fix my grammer", seen[0][1])] and seen[0][1].startswith("action-run")
    assert h.ui.stages == ["Fixed my grammar."]
    h.paste.assert_called_once_with()
    ran = h.bus.recent(types=[catalog.ACTION_RAN])[0].data
    assert ran["job_id"] == job_id and ran["ok"] is True and ran["output"] == "paste"

    _run(h.saves)
    assert not h.runtime.has_active_job
    (entry,) = h.history.entries
    assert entry["text"] == "fix my grammer" and entry["entry_kind"] == "action"
    assert entry["source_name"] == "Action · Fix grammar"


def test_the_job_slot_frees_while_a_slow_action_still_runs(h, monkeypatch):
    release = threading.Event()

    def run_once(argv, stdin, *, timeout_s, cwd="", env=None, cancel=None):
        release.wait(5)
        return 0, b"late answer", ""

    monkeypatch.setattr(programs, "run_once", run_once)

    h.runtime.run_action_text("fix", "words", source="ui")
    _run(h.jobs)
    _run(h.saves)

    assert not h.runtime.has_active_job
    assert h.ui.statuses[-1] == "Fix grammar · working…"
    release.set()
    _wait(lambda: h.ui.stages == ["late answer"])


def test_a_programs_answer_is_shown_in_the_scratchpad_never_pasted(h, monkeypatch):
    monkeypatch.setattr(programs, "run_once", lambda *a, **k: (0, b"Fixed my grammar.", ""))

    h.runtime.run_action_text("fix", "fix my grammer", source="api")
    _run(h.jobs)
    _wait(lambda: h.bus.recent(types=[catalog.ACTION_RAN]))

    assert h.ui.stages == [] and h.ui.copied == []
    h.paste.assert_not_called()
    assert h.ui.scratchpad == ["Fixed my grammar."]
    assert h.bus.recent(types=[catalog.ACTION_RAN])[0].data["output"] == "scratchpad"
    _run(h.saves)
