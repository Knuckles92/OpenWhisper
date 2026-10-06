"""A rewrite is pasted only into the app it started in, provably.

Commands and transforms paste even with auto-paste off, so the "same app"
check is their only guard. It used to pass whenever the app was unknown:
with app awareness off (the job keeps no focus) and always on Linux (no
synchronous identity), a rewrite was pasted into whatever took focus.
"""

import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtWidgets import QApplication

from services import dictation_pipeline, focus_context, synthetic_keys, text_rewrite
from services.dictation_pipeline import DictationJob, JobMode, PasteTarget
from services.focus_context import FocusSnapshot, NullCaptureService, TextContext
from services.focus_context._capture import CaptureService
from services.runtime import command, transcription
from services.runtime.command import CommandRuntime
from services.runtime.transcription import TranscriptionRuntime
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_personalize_s1_service import FakePlatform
from tests.test_personalize_s3_command_delivery import (
    BROWSER,
    NOTEPAD,
    FakeCleaner,
    FakeHistory,
    FakeService,
    FakeUI,
    Signal,
)

CHANGED_STATUS = "Rewrite copied — the app changed; paste it where you want"
UNKNOWN_STATUS = "Rewrite copied — couldn't confirm the app; paste it where you want"


@pytest.fixture
def h(monkeypatch, _session_qt_application):
    ui = FakeUI()
    history = FakeHistory()
    settings = InMemorySettings({
        SettingsKey.AUTO_PASTE: False,
        SettingsKey.COPY_CLIPBOARD: True,
        SettingsKey.APP_CONTEXT_ENABLED: False,
    })
    paste = Mock()
    for module in (transcription, command):
        monkeypatch.setattr(module, "settings_manager", settings)
    monkeypatch.setattr(transcription, "history_manager", history)
    monkeypatch.setattr(transcription, "send_paste", paste)
    monkeypatch.setattr(transcription, "is_accessibility_trusted", lambda: True)
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: True)
    monkeypatch.setattr(synthetic_keys, "wait_for_modifiers_released", lambda timeout_s=0.8: True)
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
    runtime._transcript_cleanup = FakeCleaner("CONFIDENTIAL rewrite")
    monkeypatch.setattr(runtime, "_model_info_for_history", lambda: "parakeet")
    controller.command_runtime = CommandRuntime(controller)
    yield SimpleNamespace(ui=ui, history=history, settings=settings, paste=paste,
                          submitted=submitted, controller=controller, runtime=runtime)
    controller.command_runtime.cleanup()


def _run_transform(h):
    h.controller.command_runtime.run_transform("polish")
    deadline = time.monotonic() + 2
    while not h.submitted and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.002)
    while h.submitted:
        fn, args = h.submitted.pop(0)
        fn(*args)


def _selected_in_notepad(current):
    focus_context.set_service(FakeService(
        FocusSnapshot(NOTEPAD, TextContext(selected="CONFIDENTIAL draft", selection_known=True)),
        current=current))


def test_without_app_awareness_a_transform_is_copied_when_the_app_changed(h):
    _selected_in_notepad(current=BROWSER)

    _run_transform(h)

    h.paste.assert_not_called()
    assert h.ui.copied == ["CONFIDENTIAL rewrite"]
    assert h.ui.statuses[-1] == CHANGED_STATUS
    entry, = h.history.entries
    assert "app_id" not in entry and "app_name" not in entry


def test_without_app_awareness_a_transform_still_pastes_into_the_same_app(h):
    _selected_in_notepad(current=NOTEPAD)

    _run_transform(h)

    h.paste.assert_called_once_with()
    entry, = h.history.entries
    assert "app_id" not in entry


@pytest.mark.parametrize("current, pasted", [(BROWSER, False), (NOTEPAD, True)])
def test_without_app_awareness_command_mode_checks_the_app_too(h, current, pasted):
    _selected_in_notepad(current=current)
    job = dictation_pipeline.begin_job(JobMode.COMMAND, h.settings.load_all_settings(),
                                       selection="CONFIDENTIAL draft")
    assert job.focus is None

    assert h.runtime._claim_job(job)
    h.runtime.on_transcription_complete("CONFIDENTIAL rewrite", "CONFIDENTIAL draft")

    assert h.paste.called is pasted
    assert "app_id" not in h.history.entries[0]
    if not pasted:
        assert h.ui.statuses[-1] == CHANGED_STATUS


def test_a_rewrite_whose_app_cannot_be_told_is_copied_not_pasted_blind(h):
    focus_context.set_service(NullCaptureService())
    future: Future = Future()
    future.set_result("old words")

    assert h.runtime._claim_job(DictationJob(mode=JobMode.COMMAND, selection=future))
    h.runtime.on_transcription_complete("CONFIDENTIAL rewrite", "old words")

    h.paste.assert_not_called()
    assert h.ui.copied == ["CONFIDENTIAL rewrite"]
    assert h.ui.statuses[-1] == UNKNOWN_STATUS


def test_linux_answers_the_paste_check_although_its_identity_is_asynchronous():
    platform = FakePlatform(NOTEPAD, sync=False, text=False)
    service = CaptureService(platform, deadline_s=1.0, settings=lambda: {},
                             metrics=lambda **values: None)
    focus_context.set_service(service)
    try:
        job = DictationJob(mode=JobMode.TRANSFORM,
                           target=service.request(include_text=False, include_selection=True))
        job.target.result(timeout=2)
        assert service.current_identity() is None
        assert dictation_pipeline.paste_target(job) == PasteTarget.SAME

        platform.current = BROWSER
        assert dictation_pipeline.paste_target(job) == PasteTarget.CHANGED
        assert not dictation_pipeline.paste_target_ok(job)
    finally:
        service.shutdown()


def test_a_hung_linux_identity_read_is_bounded_and_unknown(monkeypatch):
    platform = FakePlatform(NOTEPAD, sync=False, text=False)
    service = CaptureService(platform, deadline_s=1.0, settings=lambda: {},
                             metrics=lambda **values: None)
    snapshot: Future = Future()
    snapshot.set_result(FocusSnapshot(NOTEPAD))
    focus_context.set_service(service)
    monkeypatch.setattr(dictation_pipeline, "PASTE_CHECK_TIMEOUT_S", 0.05)
    platform.identity_delay = 0.5
    try:
        started = time.monotonic()
        assert dictation_pipeline.paste_target(
            DictationJob(mode=JobMode.COMMAND, target=snapshot)) == PasteTarget.UNKNOWN
        assert time.monotonic() - started < 0.4
    finally:
        service.shutdown()
