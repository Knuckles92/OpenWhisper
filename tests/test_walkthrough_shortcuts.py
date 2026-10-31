"""The Walkthrough and Screenshot shortcuts (services/runtime/walkthrough.py)."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor, QImage

from services.hotkey_manager import format_hotkey_display
from services.runtime import walkthrough as walkthrough_module
from services.runtime.walkthrough import (
    FINISH_RECORDING_FIRST,
    RECORDING_HINT,
    WalkthroughRuntime,
)
from services.settings import SettingsKey, WalkthroughSnapMode
from services.walkthrough import storage
from services.walkthrough.session import WalkthroughSession
from tests.fakes.settings import InMemorySettings


def _image(color: str = "#ff4f3a") -> QImage:
    image = QImage(24, 16, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


class Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class FakeSnap(QObject):
    """A SnapSession: emits exactly one of kept, dropped or failed."""

    kept = pyqtSignal(QImage)
    dropped = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, hint: str, take: str = "region", front: bool = False):
        super().__init__()
        self.hint = hint
        self.take = take
        self.front = front
        self.active = True
        self.keeps = self.drops = 0

    def keep(self):
        self.keeps += 1
        if self.active:
            self.active = False
            self.kept.emit(_image())

    def drop(self):
        self.drops += 1
        if self.active:
            self.active = False
            self.dropped.emit()

    def fail(self, reason: str):
        self.active = False
        self.failed.emit(reason)


class FakePanel(QObject):
    """The walkthrough's column: its buttons, and what it is told to show."""

    snap_requested = pyqtSignal()
    snap_mode_chosen = pyqtSignal(str)
    undo_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    finish_requested = pyqtSignal()
    move_requested = pyqtSignal(int, int)
    remove_requested = pyqtSignal(int)
    note_held = pyqtSignal(bool)
    pause_requested = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.notes_on = None
        self.paused = []
        self.snap_mode = ""
        self.shortcut = ""
        self.countdowns = []

    def set_snap_mode(self, mode, shortcut=None):
        self.snap_mode = mode
        if shortcut is not None:
            self.shortcut = shortcut

    def set_paused(self, paused):
        self.paused.append(paused)

    def set_notes_on(self, on):
        self.notes_on = on

    def set_countdown(self, seconds):
        self.countdowns.append(seconds)

    def add_shot(self, number, image, kept_at_s):
        self.calls.append(("add", number, kept_at_s))

    def remove_shot(self, number):
        self.calls.append(("remove", number))

    def move_shot(self, number, step):
        self.calls.append(("move", number, step))


class FakeUI:
    def __init__(self):
        self.walkthrough_panel = FakePanel()
        self.snaps = []
        self.supported = (True, "")
        self.access_requests = 0
        self.excluded = []
        self.active = []
        self.captions = []
        self.copied = []
        self.notices = []
        self.statuses = []

    def snap_support(self):
        return self.supported

    def request_snap_access(self):
        self.access_requests += 1

    def start_snap(self, *, hint="", take="region", front=False):
        snap = FakeSnap(hint, take, front)
        self.snaps.append(snap)
        return snap

    def keep_overlay_out_of_screenshots(self, enabled):
        self.excluded.append(enabled)
        return True

    def set_walkthrough_active(self, active):
        self.active.append(active)

    def show_overlay_caption(self, text):
        self.captions.append(text)

    def copy_files_to_clipboard(self, paths, text="", *, image=None):
        self.copied.append((list(paths), text, image))
        return True

    def set_status(self, text):
        self.statuses.append(text)

    def show_notice(self, text, *, done=False):
        self.notices.append((text, done))


class FakeTranscription:
    def __init__(self):
        self.recording_walkthrough = None
        self.started = []
        self.writing_up = False
        self.write_ups_canceled = 0

    def start_walkthrough(self, session, *, source):
        self.started.append(source)
        self.recording_walkthrough = session
        return True

    def cancel_write_up(self):
        if self.writing_up:
            self.write_ups_canceled += 1
        return self.writing_up


@pytest.fixture
def h(tmp_path, monkeypatch):
    settings = InMemorySettings({SettingsKey.HOTKEYS: {"screenshot": "ctrl+alt+s"}})
    monkeypatch.setattr(walkthrough_module, "settings_manager", settings)
    ui = FakeUI()
    transcription = FakeTranscription()
    recorder = SimpleNamespace(is_recording=False, capture_canceled=False,
                               get_recording_duration=lambda: 12.5)
    controller = SimpleNamespace(
        ui_controller=ui,
        transcription_runtime=transcription,
        recorder=recorder,
        recording_destination_changed=Signal(),
        cancel=Mock(),
        _refuse_dictation_during_meeting=lambda: False,
        transcription_readiness_message=lambda: None,
    )
    runtime = WalkthroughRuntime(controller)

    def stop_recording():
        # What TranscriptionRuntime does: close the session, then say so.
        session, transcription.recording_walkthrough = transcription.recording_walkthrough, None
        if session is not None:
            session.close()
        recorder.is_recording = False
        runtime.on_recording_state_changed(False)

    controller.stop_recording = Mock(side_effect=stop_recording)
    capture = tmp_path / "capture"

    def make_folder(started_at):
        capture.mkdir(exist_ok=True)
        return capture

    monkeypatch.setattr(walkthrough_module, "WalkthroughSession",
                        lambda started_at: WalkthroughSession(started_at=started_at, make_folder=make_folder))
    return SimpleNamespace(ui=ui, transcription=transcription, recorder=recorder,
                           controller=controller, runtime=runtime, settings=settings)


def _begin(h):
    h.runtime.toggle()
    session = h.transcription.recording_walkthrough
    assert session is not None
    h.recorder.is_recording = True
    return session


def test_the_shortcut_starts_a_walkthrough_kept_out_of_its_own_screenshots(h):
    _begin(h)

    assert h.transcription.started == ["hotkey"]
    assert h.ui.active == [True]
    assert h.ui.excluded == [True]


def test_another_recording_must_finish_first(h):
    h.recorder.is_recording = True

    h.runtime.toggle()

    assert h.transcription.started == []
    assert (FINISH_RECORDING_FIRST, False) in h.ui.notices


def test_a_screenshot_during_a_walkthrough_joins_it(h):
    session = _begin(h)

    h.runtime.snap()
    (snap,) = h.ui.snaps
    snap.keep()

    assert snap.hint == RECORDING_HINT
    (shot,) = session.saved_shots()
    assert shot.number == 1 and shot.kept_at_s == 12.5
    assert h.controller.recording_destination_changed.calls == [("Walkthrough · 1 shot",)]
    assert h.ui.captions == ["Screenshot 1 added"]
    assert h.ui.walkthrough_panel.calls == [("add", 1, 12.5)]
    assert h.ui.copied == []
    session.end()


def test_pressing_screenshot_again_keeps_the_one_being_drawn(h):
    session = _begin(h)

    h.runtime.snap()
    h.runtime.snap()

    assert len(h.ui.snaps) == 1
    assert h.ui.snaps[0].keeps == 1
    assert session.shot_count == 1
    session.end()


def test_finishing_while_drawing_keeps_the_screenshot_then_stops(h):
    session = _begin(h)
    h.runtime.snap()

    h.runtime.toggle()

    assert session.shot_count == 1
    h.controller.stop_recording.assert_called_once()
    assert h.ui.excluded == [True, False]
    assert h.ui.active == [True, False]
    session.end()


def test_finishing_waits_for_the_overlay_and_stops_even_if_it_drops(h):
    _begin(h)
    h.runtime.snap()
    snap = h.ui.snaps[0]
    snap.keep = Mock()  # this overlay answers later

    h.runtime.toggle()

    snap.keep.assert_called_once()
    h.controller.stop_recording.assert_not_called()
    snap.active = False
    snap.dropped.emit()
    h.controller.stop_recording.assert_called_once()


def test_a_screenshot_still_open_when_the_walkthrough_stops_goes_to_the_clipboard(h, tmp_path, monkeypatch):
    _begin(h)
    monkeypatch.setattr(storage, "new_screenshot_path", lambda taken_at: tmp_path / "plain.png")
    h.runtime.snap()

    h.controller.stop_recording()

    (snap,) = h.ui.snaps
    assert snap.keeps == 1
    ((paths, text, image),) = h.ui.copied
    assert paths == [tmp_path / "plain.png"] and text == str(tmp_path / "plain.png")
    assert not image.isNull()
    assert ("Screenshot copied — the walkthrough had already stopped", True) in h.ui.notices


def test_canceling_drops_the_screenshot_being_drawn(h):
    _begin(h)
    h.runtime.snap()
    h.recorder.capture_canceled = True

    h.transcription.recording_walkthrough = None
    h.runtime.on_recording_state_changed(False)

    assert h.ui.snaps[0].drops == 1
    assert h.ui.copied == []


def test_a_plain_screenshot_is_copied_as_a_picture_a_file_and_its_path(h, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "new_screenshot_path", lambda taken_at: tmp_path / "plain.png")

    h.runtime.snap()
    (snap,) = h.ui.snaps
    snap.keep()

    assert snap.hint == ""
    ((paths, text, image),) = h.ui.copied
    assert paths == [tmp_path / "plain.png"] and (tmp_path / "plain.png").exists()
    assert text == str(tmp_path / "plain.png")
    assert image.width() == 24
    assert ("Screenshot copied", True) in h.ui.notices
    assert h.controller.recording_destination_changed.calls == []


def test_a_desktop_without_screenshots_says_why(h):
    h.ui.supported = (False, "Install grim to take screenshots on Hyprland.")

    h.runtime.snap()

    assert h.ui.snaps == []
    assert ("Install grim to take screenshots on Hyprland.", False) in h.ui.notices
    # Where the system can grant what's missing (macOS), it is asked.
    assert h.ui.access_requests == 1


def test_a_failed_capture_says_why(h):
    h.runtime.snap()

    h.ui.snaps[0].fail("The screen couldn't be captured")

    assert ("The screen couldn't be captured", False) in h.ui.notices
    h.runtime.snap()
    assert len(h.ui.snaps) == 2


def test_the_tray_lets_its_menu_close_before_the_screen_is_grabbed(h):
    h.runtime.snap(source="tray")

    assert h.ui.snaps == []
    assert h.runtime._tray_timer.isActive()
    h.runtime._tray_timer.stop()
    h.runtime._tray_timer.timeout.emit()
    assert len(h.ui.snaps) == 1


def test_a_recording_that_was_not_a_walkthrough_ending_changes_nothing(h):
    h.runtime.on_recording_state_changed(False)

    assert h.ui.excluded == [] and h.ui.active == []


def test_the_destination_counts_screenshots():
    assert walkthrough_module.shots_label(0) == "Walkthrough"
    assert walkthrough_module.shots_label(1) == "Walkthrough · 1 shot"
    assert walkthrough_module.shots_label(3) == "Walkthrough · 3 shots"


def test_sessions_start_now(h):
    before = datetime.now()

    session = _begin(h)

    assert session.started_at >= before


def test_a_screenshot_that_cannot_be_saved_says_so_and_the_walkthrough_goes_on(h, monkeypatch):
    import errno

    def read_only(started_at):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(walkthrough_module, "WalkthroughSession",
                        lambda started_at: WalkthroughSession(started_at=started_at,
                                                              make_folder=read_only))
    session = _begin(h)

    h.runtime.snap()
    h.ui.snaps[0].keep()

    assert session.open
    assert h.ui.captions == []
    assert h.ui.copied == []
    assert ("Couldn't save the screenshot (Permission denied) — the walkthrough keeps recording",
            False) in h.ui.notices
    h.controller.stop_recording.assert_not_called()


def test_a_plain_screenshot_without_a_writable_folder_is_still_copied(h, monkeypatch):
    def read_only(taken_at):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(storage, "new_screenshot_path", read_only)

    h.runtime.snap()
    h.ui.snaps[0].keep()

    ((paths, text, _image),) = h.ui.copied
    assert paths == [] and text == ""
    assert ("Screenshot copied (its file couldn't be saved)", False) in h.ui.notices


# --- the walkthrough's column ------------------------------------------------


def _keep_snap(h):
    h.ui.walkthrough_panel.snap_requested.emit()
    h.ui.snaps[-1].keep()


def test_the_columns_snap_and_done_take_a_screenshot_then_finish(h):
    session = _begin(h)

    _keep_snap(h)
    h.ui.walkthrough_panel.finish_requested.emit()

    assert session.shot_count == 1
    h.controller.stop_recording.assert_called_once()
    # Done again, once it is over, starts nothing.
    h.ui.walkthrough_panel.finish_requested.emit()
    h.controller.stop_recording.assert_called_once()
    assert h.transcription.started == ["hotkey"]


def test_the_columns_cancel_is_the_users_cancel(h):
    _begin(h)

    h.ui.walkthrough_panel.cancel_requested.emit()

    h.controller.cancel.assert_called_once_with()


def test_the_columns_cancel_stops_its_write_up_not_a_dictation(h):
    _begin(h)
    h.transcription.writing_up = True

    h.ui.walkthrough_panel.cancel_requested.emit()

    assert h.transcription.write_ups_canceled == 1
    h.controller.cancel.assert_not_called()


def test_a_new_walkthrough_waits_for_the_last_ones_write_up(h):
    h.transcription.writing_up = True

    h.runtime.toggle()

    assert h.transcription.started == []
    assert walkthrough_module.WAIT_FOR_WRITE_UP in h.ui.statuses


def test_undo_takes_the_newest_screenshot_out_and_says_so(h):
    session = _begin(h)
    _keep_snap(h)
    _keep_snap(h)
    panel = h.ui.walkthrough_panel

    panel.undo_requested.emit()

    assert session.shot_count == 1
    assert panel.calls[-1] == ("remove", 2)
    assert h.controller.recording_destination_changed.calls[-1] == ("Walkthrough · 1 shot",)
    session.end()


def test_moving_and_removing_reach_the_walkthrough_and_its_column(h):
    session = _begin(h)
    for _ in range(3):
        _keep_snap(h)
    panel = h.ui.walkthrough_panel

    panel.move_requested.emit(3, -1)
    panel.move_requested.emit(1, -1)  # already first: nothing moves
    panel.remove_requested.emit(1)

    assert panel.calls[3:] == [("move", 3, -1), ("remove", 1)]
    assert [shot.path.name for shot in session.saved_shots()] == ["shot-3.png", "shot-2.png"]
    assert h.controller.recording_destination_changed.calls[-1] == ("Walkthrough · 2 shots",)
    session.end()


def test_the_column_is_answered_once_across_walkthroughs(h):
    _begin(h)
    h.controller.stop_recording()
    _begin(h)

    h.ui.walkthrough_panel.snap_requested.emit()

    assert len(h.ui.snaps) == 1


def test_without_a_column_screenshots_still_join_the_walkthrough(h):
    h.ui.walkthrough_panel = None
    session = _begin(h)

    h.runtime.snap()
    h.ui.snaps[0].keep()

    assert session.shot_count == 1
    assert h.ui.captions == ["Screenshot 1 added"]
    session.end()


# --- what Snap takes -----------------------------------------------------------


def test_the_shortcut_repeats_the_kind_last_picked(h):
    h.settings.values[SettingsKey.WALKTHROUGH_SNAP_MODE] = WalkthroughSnapMode.SCREEN
    session = _begin(h)

    h.runtime.snap()

    (snap,) = h.ui.snaps
    assert (snap.take, snap.front) == ("screen", False)
    session.end()


def test_the_columns_snap_means_the_window_in_front_not_the_column(h):
    h.settings.values[SettingsKey.WALKTHROUGH_SNAP_MODE] = WalkthroughSnapMode.WINDOW
    session = _begin(h)

    h.ui.walkthrough_panel.snap_requested.emit()

    (snap,) = h.ui.snaps
    assert (snap.take, snap.front) == ("window", True)
    session.end()


def test_a_pick_from_the_menu_is_taken_now_and_remembered(h):
    session = _begin(h)
    panel = h.ui.walkthrough_panel
    shown = format_hotkey_display("ctrl+alt+s")  # ⌃⌥S on macOS
    assert (panel.snap_mode, panel.shortcut) == ("region", shown)

    panel.snap_mode_chosen.emit(WalkthroughSnapMode.WINDOW)

    assert h.settings.values[SettingsKey.WALKTHROUGH_SNAP_MODE] == "window"
    assert panel.snap_mode == "window"
    (snap,) = h.ui.snaps
    assert snap.take == "window"
    session.end()


def test_outside_a_walkthrough_the_shortcut_draws_a_region(h):
    h.settings.values[SettingsKey.WALKTHROUGH_SNAP_MODE] = WalkthroughSnapMode.SCREEN

    h.runtime.snap()

    assert h.ui.snaps[0].take == "region"


def test_in_3_seconds_counts_down_then_opens_the_overlay(h):
    h.settings.values[SettingsKey.WALKTHROUGH_SNAP_MODE] = WalkthroughSnapMode.DELAYED
    session = _begin(h)

    h.runtime.snap()
    assert h.ui.snaps == []
    for _ in range(3):
        h.runtime._count_down()

    (snap,) = h.ui.snaps
    assert snap.take == "region"
    assert h.ui.walkthrough_panel.countdowns == [3, 2, 1, 0]
    assert h.ui.captions[:3] == ["Snapping in 3…", "Snapping in 2…", "Snapping in 1…"]
    assert not h.runtime._countdown.isActive()
    session.end()


def test_snap_pressed_again_during_the_countdown_takes_it_now(h):
    h.settings.values[SettingsKey.WALKTHROUGH_SNAP_MODE] = WalkthroughSnapMode.DELAYED
    session = _begin(h)

    h.runtime.snap()
    h.runtime.snap()

    assert [snap.take for snap in h.ui.snaps] == ["region"]
    assert not h.runtime._countdown.isActive()
    session.end()


def test_a_countdown_ends_with_its_walkthrough(h):
    h.settings.values[SettingsKey.WALKTHROUGH_SNAP_MODE] = WalkthroughSnapMode.DELAYED
    _begin(h)
    h.runtime.snap()

    h.controller.stop_recording()

    assert not h.runtime._countdown.isActive()
    assert h.ui.walkthrough_panel.countdowns[-1] == 0
    assert h.ui.snaps == []


# --- Hold for note ---------------------------------------------------------------


def test_holding_for_a_note_marks_the_recording_where_it_is(h):
    session = _begin(h)

    h.recorder.get_recording_duration = lambda: 3.0
    h.ui.walkthrough_panel.note_held.emit(True)
    h.recorder.get_recording_duration = lambda: 5.5
    h.ui.walkthrough_panel.note_held.emit(False)

    assert session.storyboard().notes_s == ((3.0, 5.5),)
    session.end()


def test_notes_are_offered_only_with_an_ai_write_up(h):
    _begin(h)
    assert h.ui.walkthrough_panel.notes_on is False
    h.controller.stop_recording()

    h.settings.values[SettingsKey.WALKTHROUGH_WRITE_UP] = True
    _begin(h)

    assert h.ui.walkthrough_panel.notes_on is True


# --- Pause -------------------------------------------------------------------------


def test_pause_and_resume_reach_the_recorder_and_the_column(h):
    session = _begin(h)
    h.recorder.pause = Mock(return_value=True)
    h.recorder.resume = Mock()
    h.controller.status_update = Signal()
    panel = h.ui.walkthrough_panel

    panel.pause_requested.emit(True)
    panel.pause_requested.emit(False)

    h.recorder.pause.assert_called_once_with()
    h.recorder.resume.assert_called_once_with()
    assert panel.paused == [True, False]
    assert h.controller.status_update.calls == [("Walkthrough · Paused",),
                                                 ("Walkthrough · Recording...",)]
    session.end()


def test_a_recording_that_wont_pause_leaves_the_column_recording(h):
    session = _begin(h)
    h.recorder.pause = Mock(return_value=False)
    h.controller.status_update = Signal()

    h.ui.walkthrough_panel.pause_requested.emit(True)

    assert h.ui.walkthrough_panel.paused == []
    session.end()
