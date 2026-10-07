"""Command Mode, transform and paste-original outcomes show near the pointer.

They run from another app while OpenWhisper sits in the tray, where its status
line is out of sight, so each outcome also gets a short overlay notice.
"""

import importlib
import os
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from services import focus_context, text_rewrite
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import FocusSnapshot
from services.runtime import command
from ui_qt.overlay_state import OverlayState
from ui_qt.overlays import moments, waveform_overlay
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from tests.test_personalize_s3_command_delivery import (  # noqa: F401  (fixtures)
    BROWSER, NOTEPAD, FakeCleaner, FakeService, _command_job, _qapp, _run_submitted, h,
)


@pytest.fixture
def notices(h):  # noqa: F811
    shown = []
    h.ui.show_notice = lambda text, done=False: shown.append((text, done))
    return shown


@pytest.fixture
def overlay():
    widget = WaveformOverlay()
    yield widget
    widget.hide()
    widget.close()


def _paint(widget):
    with patch.object(waveform_overlay.logger, "error") as error:
        widget.grab()
    error.assert_not_called()


# --- the overlay -----------------------------------------------------------------------


def test_a_notice_shows_while_hidden_and_then_hides(overlay):
    overlay.show_notice("Select the text to change first")
    assert overlay.isVisible() and overlay.current_state == overlay.STATE_NOTICE
    assert overlay._notice == "Select the text to change first"
    assert overlay.hidden_timer.isActive()
    assert overlay.hidden_timer.interval() >= waveform_overlay.CAPTION_MS
    _paint(overlay)
    overlay.hidden_timer.timeout.emit()
    assert not overlay.isVisible() and overlay._notice == ""


def test_long_notices_stay_up_longer_and_still_paint(overlay):
    overlay.show_notice("Rewrite copied — couldn't confirm the app; paste it where you want")
    long_wait = overlay.hidden_timer.interval()
    _paint(overlay)
    overlay.show_notice("Rewritten", done=True)
    assert overlay.hidden_timer.interval() < long_wait
    _paint(overlay)


def test_a_finished_command_bursts_after_its_sparkles_gather_and_replays(overlay):
    overlay.show_notice("Rewritten", done=True)
    overlay.timer.stop()
    overlay.animation_time = moments.MOMENTS["done"].pop_at / 2
    overlay._update_animation()
    assert overlay.stt_particles == []
    overlay.animation_time = moments.MOMENTS["done"].pop_at
    overlay._update_animation()
    assert overlay.stt_particles
    for moment in (0.1, 0.3, 0.6, 1.5, overlay._notice_ms() / 1000):
        overlay.animation_time = moment
        _paint(overlay)

    overlay.show_notice("Inserted", done=True)
    assert overlay.animation_time == 0.0 and overlay.stt_particles == []
    overlay.show_notice("Nothing to paste yet")
    overlay.animation_time = 1.0
    overlay._update_animation()
    assert overlay.stt_particles == []


def test_a_notice_too_long_for_the_overlay_ends_in_an_ellipsis(overlay):
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QFont, QFontMetrics

    overlay.show_notice("Error: The AI model failed: " + "rate limit reached " * 10)
    font, rect = QFont("Segoe UI", 10, QFont.Weight.DemiBold), QRect(0, 0, 240, 64)
    shown = overlay._fit_notice(font, rect)
    assert shown.endswith("…") and shown.startswith("Error: The AI model failed:")
    flags = int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap)
    assert QFontMetrics(font).boundingRect(rect, flags, shown).height() <= rect.height()


def test_a_recording_in_progress_keeps_its_overlay(overlay):
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    overlay.show_notice("Nothing to paste yet")
    assert overlay.current_state == overlay.STATE_RECORDING
    assert overlay._caption == "Nothing to paste yet"

    overlay.set_state(overlay.STATE_TRANSCRIBING)
    overlay.show_notice("Nothing to paste yet")
    assert overlay.current_state == overlay.STATE_TRANSCRIBING


def test_the_main_window_in_front_gets_no_notice():
    from ui_qt.ui_controller import UIController

    ui = SimpleNamespace(main_window=Mock(), overlay=Mock())
    ui.main_window.isActiveWindow.return_value = True
    UIController.show_notice(ui, "Nothing to paste yet")
    ui.overlay.show_notice.assert_not_called()

    ui.main_window.isActiveWindow.return_value = False
    UIController.show_notice(ui, "Original pasted", done=True)
    ui.overlay.show_notice.assert_called_once_with("Original pasted", done=True)


# --- Command Mode and transforms ------------------------------------------------------


def test_a_refused_command_says_why_after_the_overlay_hides(h, notices):  # noqa: F811
    h.runtime._transcript_cleanup = FakeCleaner(text_rewrite.NEEDS_SELECTION)
    h.controller.overlay_state_update.handler = lambda state: notices.append(state)

    assert h.runtime._claim_job(_command_job(""))
    h.runtime.transcribe_audio_file(h.audio)

    assert notices[-2:] == [OverlayState.NONE, ("Select the text to change first", False)]


def test_a_rewrite_for_an_app_that_lost_focus_says_it_was_copied(h, notices):  # noqa: F811
    h.runtime._transcript_cleanup = FakeCleaner("Rewritten.")
    focus_context.set_service(FakeService(FocusSnapshot(), current=BROWSER))

    assert h.runtime._claim_job(_command_job("old", FocusSnapshot(NOTEPAD)))
    h.runtime.transcribe_audio_file(h.audio)

    assert notices == [("Rewrite copied — the app changed; paste it where you want", False)]


def test_a_pasted_rewrite_and_a_written_command_are_confirmed(h, notices):  # noqa: F811
    focus_context.set_service(FakeService(FocusSnapshot(), current=NOTEPAD))
    h.runtime._transcript_cleanup = FakeCleaner("Dear Sir or Madam,")
    assert h.runtime._claim_job(_command_job("hey you", FocusSnapshot(NOTEPAD)))
    h.runtime.transcribe_audio_file(h.audio)
    assert notices == [("Rewritten", True)]

    h.runtime._transcript_cleanup = FakeCleaner("Thanks for the notes.")
    assert h.runtime._claim_job(_command_job("", FocusSnapshot(NOTEPAD)))
    h.runtime.transcribe_audio_file(h.audio)
    assert notices[-1] == ("Inserted", True)


def test_a_failed_paste_of_a_rewrite_is_reported(h, notices):  # noqa: F811
    focus_context.set_service(FakeService(FocusSnapshot(), current=NOTEPAD))
    h.runtime._transcript_cleanup = FakeCleaner("Formal.")
    h.paste.side_effect = OSError("no input desktop")
    h.ui.commit_transcript_clipboard = lambda stage, text: True
    assert h.runtime._claim_job(_command_job("hey", FocusSnapshot(NOTEPAD)))
    h.runtime.transcribe_audio_file(h.audio)
    assert notices == [(h.ui.statuses[-1], False)]
    assert "paste failed" in notices[0][0]


def test_a_failed_paste_of_the_original_is_reported(h, notices):  # noqa: F811
    h.paste.side_effect = OSError("no input desktop")
    h.ui.commit_transcript_clipboard = lambda stage, text: True
    assert not h.runtime.paste_text_now("um hello")
    assert notices == [("Transcription complete (paste failed)", False)]


def test_an_ai_failure_in_a_command_is_reported(h, notices):  # noqa: F811
    cleaner = FakeCleaner("")
    cleaner.cleanup = lambda text, system_prompt=None, timeout_s=None, deadline_s=None: setattr(
        cleaner, "last_error", "timed out after 20s") or ""
    h.runtime._transcript_cleanup = cleaner
    assert h.runtime._claim_job(_command_job("hey", FocusSnapshot(NOTEPAD)))
    h.runtime.transcribe_audio_file(h.audio)
    assert notices == [(f"Error: {text_rewrite.TIMED_OUT_MESSAGE}", False)]


def test_a_transform_with_nothing_selected_says_so(h, notices):  # noqa: F811
    h.controller.command_runtime.run_transform("polish")
    deadline = time.monotonic() + 2
    while not notices and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.002)
    assert notices == [("Select text to transform", False)]


def test_transforms_and_command_mode_without_a_provider_say_so(h, notices, monkeypatch):  # noqa: F811
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: False)
    h.controller.command_runtime.run_transform("polish")
    h.controller.command_runtime.run_transform("no-such-transform")
    h.controller.command_runtime.key_pressed(time.monotonic())
    QApplication.processEvents()
    assert notices == [
        ("Set up AI cleanup to use transforms", False),
        ("That transform no longer exists", False),
        (command.NO_PROVIDER_MESSAGE, False),
    ]


def test_a_command_during_a_recording_says_to_finish_it(h, notices):  # noqa: F811
    h.controller.recorder = SimpleNamespace(is_recording=True)
    h.controller.command_runtime.key_pressed(time.monotonic())
    QApplication.processEvents()
    assert notices == [("Finish the current recording before using Command Mode", False)]


def test_plain_dictation_gets_no_notice(h, notices):  # noqa: F811
    h.runtime._transcript_cleanup = FakeCleaner("Hello.")
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))
    h.runtime.transcribe_audio_file(h.audio)
    assert h.ui.copied == ["make it formal"]
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))
    h.runtime.on_transcription_error("The engine crashed")
    assert notices == []


def test_a_canceled_rewrite_gets_no_notice(h, notices):  # noqa: F811
    assert h.runtime._claim_job(_command_job("hey", FocusSnapshot(NOTEPAD)))
    h.runtime._cancel_requested.set()
    h.runtime.on_transcription_error("Transcription canceled")
    assert notices == []


def test_a_transform_canceled_while_its_selection_is_read_gets_no_notice(h, notices):  # noqa: F811
    focus_context.set_service(FakeService(FocusSnapshot(), current=NOTEPAD))
    h.controller.current_backend.is_transcribing = False
    h.controller.command_runtime.run_transform("polish")
    assert h.runtime.has_active_job
    h.runtime.cancel()
    deadline = time.monotonic() + 2
    while h.runtime.has_active_job and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.002)
    assert not h.runtime.has_active_job
    assert notices == []


def test_busy_refusals_of_rewrites_and_pastes_show_by_the_pointer(h, notices):  # noqa: F811
    busy = "A transcription is already in progress — wait before "
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))
    assert not h.runtime.begin_rewrite_job(
        DictationJob(mode=JobMode.TRANSFORM), source_name="Transform · Polish")
    assert not h.runtime.paste_text_now("um hello")
    assert notices == [(busy + "rewriting text", False), (busy + "pasting", False)]


# --- Paste and copy original ----------------------------------------------------------


def _actions():
    return importlib.import_module("ui_qt.history_actions")


class HistoryUI:
    def __init__(self, pastes=True, copies=True):
        self.statuses, self.notices = [], []
        self._pastes, self._copies = pastes, copies

    def set_status(self, text):
        self.statuses.append(text)

    def show_notice(self, text, done=False):
        self.notices.append((text, done))

    def refresh_history(self):
        pass

    def on_paste_text_now(self, text):
        return self._pastes

    def copy_to_clipboard(self, text):
        return self._copies


@pytest.fixture(autouse=True)
def quiet_sync(monkeypatch):
    sync_module = importlib.import_module("services.remote_records.sync")
    fake = type("Sync", (), {
        "record_saved": lambda *_a: None,
        "record_edited": lambda *_a: None,
        "record_deleted": lambda *_a: None,
    })()
    monkeypatch.setattr(sync_module.record_sync, "_instance", fake)


def _dictation(text, raw=None):
    history = importlib.import_module("services.history_manager").history_manager
    return history.add_entry(text=text, raw_text=raw, model="base",
                             entry_kind="dictation", source_name="Quick Record")


def test_paste_and_copy_original_report_where_the_user_is():
    ui = HistoryUI()
    _actions().paste_last_original(ui)
    _actions().copy_last_original(ui)
    assert ui.notices == [("Nothing to paste yet", False), ("Nothing to copy yet", False)]

    _dictation("as heard")
    ui = HistoryUI()
    _actions().paste_last_original(ui)
    assert ui.notices == [(_actions().NOT_EDITED, False)]

    _dictation("Hello, world.", raw="um hello world")
    ui = HistoryUI()
    _actions().paste_last_original(ui)
    _actions().copy_last_original(ui)
    paste_key = "Cmd+V" if os.sys.platform == "darwin" else "Ctrl+V"
    assert ui.notices == [("Original pasted", True), (f"Copied — paste with {paste_key}", True)]
    assert ui.statuses == [_actions().PASTED, _actions().COPIED]

    ui = HistoryUI(copies=False)
    _actions().copy_last_original(ui)
    assert ui.notices == [(_actions().COPY_FAILED, False)]
