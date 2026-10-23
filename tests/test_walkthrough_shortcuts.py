"""The Walkthrough and Screenshot shortcuts (services/runtime/walkthrough.py)."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor, QImage

from services.runtime import walkthrough as walkthrough_module
from services.runtime.walkthrough import (
    FINISH_RECORDING_FIRST,
    RECORDING_HINT,
    WalkthroughRuntime,
)
from services.walkthrough import storage
from services.walkthrough.session import WalkthroughSession


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

    def __init__(self, hint: str):
        super().__init__()
        self.hint = hint
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


class FakeUI:
    def __init__(self):
        self.snaps = []
        self.supported = (True, "")
        self.excluded = []
        self.active = []
        self.captions = []
        self.column = []
        self.copied = []
        self.notices = []
        self.statuses = []

    def snap_support(self):
        return self.supported

    def start_snap(self, *, hint=""):
        snap = FakeSnap(hint)
        self.snaps.append(snap)
        return snap

    def keep_overlay_out_of_screenshots(self, enabled):
        self.excluded.append(enabled)
        return True

    def set_walkthrough_active(self, active):
        self.active.append(active)

    def show_overlay_caption(self, text):
        self.captions.append(text)

    def add_walkthrough_shot(self, number, image):
        self.column.append((number, image))

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

    def start_walkthrough(self, session, *, source):
        self.started.append(source)
        self.recording_walkthrough = session
        return True


@pytest.fixture
def h(tmp_path, monkeypatch):
    ui = FakeUI()
    transcription = FakeTranscription()
    recorder = SimpleNamespace(is_recording=False, capture_canceled=False,
                               get_recording_duration=lambda: 12.5)
    controller = SimpleNamespace(
        ui_controller=ui,
        transcription_runtime=transcription,
        recorder=recorder,
        recording_destination_changed=Signal(),
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
                           controller=controller, runtime=runtime)


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
    assert [number for number, _image in h.ui.column] == [1]
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
