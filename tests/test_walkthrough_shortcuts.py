"""The Walkthrough and Screenshot shortcuts (services/runtime/walkthrough.py)."""

import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QCoreApplication, QObject, QPointF, QRect, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QImage

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
        self.auto_mode = False

    def set_snap_mode(self, mode, shortcut=None):
        self.snap_mode = mode
        if shortcut is not None:
            self.shortcut = shortcut

    def set_paused(self, paused):
        self.paused.append(paused)

    def set_notes_on(self, on):
        self.notes_on = on

    def set_auto_mode(self, enabled):
        self.auto_mode = enabled

    def set_countdown(self, seconds):
        self.countdowns.append(seconds)

    def add_shot(self, number, image, kept_at_s, *, source_rect=None):
        self.calls.append(("add", number, kept_at_s))

    def remove_shot(self, number):
        self.calls.append(("remove", number))

    def move_shot(self, number, step):
        self.calls.append(("move", number, step))


class FakeDrawing(QObject):
    arming = pyqtSignal()
    drawn = pyqtSignal(object, object)
    enabled_changed = pyqtSignal(bool)
    capture_changed = pyqtSignal()
    capture_delay_changed = pyqtSignal(int)
    capture_cancelled = pyqtSignal()
    interaction_changed = pyqtSignal(bool)

    def __init__(self, mark_shortcuts):
        super().__init__()
        self.mark_shortcuts = mark_shortcuts
        self.armed = False
        self.enabled = True
        self.busy = False
        self.mark_actions = []
        self.capture_region = None
        self.interacting = False
        self.pending = False
        self.countdown = 0
        self.captured = False

    def set_capture_countdown(self, remaining, total=0):
        self.countdown = remaining

    def set_capture_pending(self, pending, *, captured=False, screen=None):
        self.pending = pending
        self.captured = captured
        if not pending:
            self.captured_screen = screen

    def arm(self):
        if self.enabled and not self.busy:
            self.arming.emit()
            self.armed = True

    def set_enabled(self, enabled):
        if self.enabled == enabled:
            return
        self.enabled = enabled
        if not enabled:
            self.armed = False
        self.enabled_changed.emit(enabled)

    def handle_mark_shortcut(self, action):
        if self.enabled:
            self.mark_actions.append(action)

    def pinned_at(self, position):
        return ()

    def pinned_on(self, screen):
        return ()

    def set_busy(self, busy):
        self.busy = busy


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
        self.drawing = None

    def snap_support(self):
        return self.supported

    def request_snap_access(self):
        self.access_requests += 1

    def start_snap(self, *, hint="", take="region", front=False, click_at=None, screen=None):
        snap = FakeSnap(hint, take, front)
        snap.click_at = click_at
        snap.screen = screen
        self.snaps.append(snap)
        return snap

    def start_walkthrough_drawing(self, *, mark_shortcuts):
        self.drawing = FakeDrawing(mark_shortcuts)
        return self.drawing

    def stop_walkthrough_drawing(self):
        self.drawing.set_enabled(False)
        self.drawing = None

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
    recorder = SimpleNamespace(is_recording=False, capture_canceled=False, paused=False,
                               get_recording_duration=lambda: 12.5)
    controller = SimpleNamespace(
        hotkey_manager=Mock(hotkeys={}),
        ui_controller=ui,
        transcription_runtime=transcription,
        recorder=recorder,
        recording_destination_changed=Signal(),
        status_update=Signal(),
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


@pytest.fixture(params=["win32", "darwin"])
def auto(h, monkeypatch, request):
    h.settings.values[SettingsKey.WALKTHROUGH_AUTO_MODE] = True
    monkeypatch.setattr(walkthrough_module, "sys", SimpleNamespace(platform=request.param))
    hook = Mock()
    hook.wait_until_installed.return_value = True
    factory = Mock(return_value=hook)
    name = "MouseClickMonitor" if request.param == "darwin" else "MouseButtonHook"
    monkeypatch.setattr(walkthrough_module, name, factory)
    session = _begin(h)
    on_click = factory.call_args.kwargs["on_click"]

    def click(x, y):
        on_click(x, y, time.monotonic())
        QCoreApplication.processEvents()

    yield SimpleNamespace(session=session, click=click, hook=hook)
    h.runtime.cleanup()
    session.end()


def test_auto_mode_coalesces_clicks_and_finishes_after_the_pending_shot(h, auto):
    assert h.ui.walkthrough_panel.auto_mode
    auto.click(20, 40)
    auto.click(-120, 25)
    h.runtime.finish()
    assert h.runtime._auto_timer.isActive()
    assert not h.ui.snaps
    h.controller.stop_recording.assert_not_called()

    h.runtime._auto_timer.timeout.emit()
    (snap,) = h.ui.snaps
    assert snap.take == WalkthroughSnapMode.SCREEN
    assert snap.click_at == (-120, 25)
    snap.keep()

    (shot,) = auto.session.saved_shots()
    assert shot.kept_at_s == 12.5
    assert h.ui.walkthrough_panel.calls == [("add", 1, 12.5)]
    assert not h.ui.copied
    h.controller.stop_recording.assert_called_once()
    auto.hook.stop.assert_called_once()
    assert not h.ui.walkthrough_panel.auto_mode


@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
def test_auto_region_crops_clicks_and_drawings_with_marks_at_device_resolution(h, auto, dpr):
    from ui_qt.snap import export
    from ui_qt.snap.annotations import Step

    screen = SimpleNamespace(geometry=lambda: QRect(-1920, -500, 1920, 1080))
    rect = QRect(6, 4, 12, 10)
    drawing = h.ui.drawing
    drawing.capture_region = (screen, rect)
    marks = (Step(QPointF(12, 8), 1, QColor("blue")),)
    drawing.pinned_on = Mock(return_value=marks)
    image = QImage(round(24 * dpr), round(16 * dpr), QImage.Format.Format_RGB32)
    image.setDevicePixelRatio(dpr)
    image.fill(QColor("#194754"))
    expected = export.render(image, rect, marks)
    keep_shot = Mock(wraps=h.runtime._keep_walkthrough_shot)
    h.runtime._keep_walkthrough_shot = keep_shot

    for make_capture in (lambda: auto.click(-120, 25), lambda: drawing.drawn.emit(screen, marks)):
        make_capture()
        if h.runtime._auto_timer.isActive():
            h.runtime._auto_timer.timeout.emit()
        snap = h.ui.snaps[-1]
        assert snap.screen is screen
        snap.active = False
        snap.capture_rect = screen.geometry()
        snap.kept.emit(image)
        shot = auto.session.saved_shots()[-1]
        saved = QImage(str(shot.path))
        assert saved.size() == expected.size()
        assert all(saved.pixelColor(x, y) == expected.pixelColor(x, y)
                   for x in range(saved.width()) for y in range(saved.height()))
        assert keep_shot.call_args.kwargs["source_rect"] == QRect(-1914, -496, 12, 10)
    drawing.pinned_on.assert_called_once_with(screen)


def test_closing_auto_region_discards_pending_capture_and_queued_close_click(h, auto):
    drawing = h.ui.drawing
    drawing.capture_region = (QGuiApplication.primaryScreen(), QRect(2, 2, 12, 10))
    auto.click(20, 40)
    h.runtime._auto_timer.timeout.emit()
    pending = h.ui.snaps[-1]
    close_at = time.monotonic() - 0.01  # A hook release already queued before Qt closes ×.
    drawing.capture_region = None
    drawing.capture_changed.emit()
    h.runtime._on_click(auto.session, 20, 40, close_at)
    h.runtime._auto_timer.timeout.emit()
    assert pending.drops == 1
    assert len(h.ui.snaps) == 1
    assert not auto.session.saved_shots()
    assert not h.runtime._auto_timer.isActive()

    auto.click(-120, 25)
    h.runtime._auto_timer.timeout.emit()
    assert h.ui.snaps[-1].click_at == (-120, 25)
    h.ui.snaps[-1].keep()
    assert len(auto.session.saved_shots()) == 1


def test_closing_region_while_finishing_does_not_leave_recording_waiting(h, auto):
    auto.click(20, 40)
    h.runtime.finish()
    h.controller.stop_recording.assert_not_called()
    h.ui.drawing.capture_region = None
    h.ui.drawing.capture_changed.emit()
    h.controller.stop_recording.assert_called_once()
    assert not h.runtime._auto_timer.isActive()
    assert not auto.session.saved_shots()


def test_auto_mark_shortcuts_follow_the_live_toolbar(h, auto):
    drawing = h.ui.drawing
    register = h.controller.hotkey_manager.set_dynamic_hotkeys
    family, keys, on_shortcut = register.call_args.args
    assert keys == {"keep_marks": "f8", "clear": "f9"}
    assert drawing.mark_shortcuts == {"keep_marks": "F8", "clear": "F9"}
    assert not drawing.armed

    on_shortcut("keep_marks")
    on_shortcut("clear")
    assert not drawing.mark_actions
    QCoreApplication.processEvents()
    assert drawing.mark_actions == ["keep_marks", "clear"]

    h.runtime.snap()
    assert register.call_args.args[:2] == (family, {})
    h.ui.snaps[-1].drop()
    assert register.call_args.args[:2] == (family, keys)

    on_shortcut("clear")
    h.controller.stop_recording()
    assert register.call_args.args[:2] == (family, {})
    QCoreApplication.processEvents()
    assert drawing.mark_actions == ["keep_marks", "clear"]


def test_mark_shortcuts_respect_an_existing_binding(h):
    session = _begin(h)
    h.controller.hotkey_manager.hotkeys["screenshot"] = "f8"
    try:
        h.runtime._start_auto_drawing(session, h.settings.load_all_settings())
        assert h.ui.drawing.mark_shortcuts == {"clear": "F9"}
        registered = h.controller.hotkey_manager.set_dynamic_hotkeys.call_args.args[1]
        assert registered == {"clear": "f9"}
    finally:
        h.controller.stop_recording()


def test_auto_draw_takes_priority_over_a_pending_click(h, auto):
    from PyQt6.QtCore import QPointF

    from ui_qt.snap.annotations import Step

    auto.click(20, 40)
    h.ui.walkthrough_panel.snap_requested.emit()
    assert h.ui.drawing.armed
    assert not h.ui.snaps
    assert not h.runtime._auto_timer.isActive()

    auto.click(22, 42)
    h.runtime._auto_timer.timeout.emit()
    assert not h.ui.snaps

    screen = object()
    marks = (Step(QPointF(12, 8), 1, QColor("blue")),)
    h.ui.drawing.drawn.emit(screen, marks)
    (snap,) = h.ui.snaps
    assert snap.take == WalkthroughSnapMode.SCREEN
    assert snap.screen is screen
    snap.keep()
    assert h.ui.drawing.armed
    assert not h.ui.drawing.busy

    h.ui.drawing.drawn.emit(screen, marks)
    _, next_snap = h.ui.snaps
    h.runtime.snap()
    assert h.ui.snaps == [snap, next_snap]
    h.recorder.get_recording_duration = lambda: 13.0
    next_snap.keep()
    _, shot = auto.session.saved_shots()
    assert QImage(str(shot.path)).pixelColor(6, 8) != _image().pixelColor(6, 8)
    assert shot.kept_at_s == 12.5
    assert h.ui.snaps[2].take == WalkthroughSnapMode.REGION


def test_delayed_drawings_restart_countdown_and_keep_one_combined_shot(h, auto):
    from ui_qt.snap.annotations import Step

    h.settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] = 3
    drawing = h.ui.drawing
    drawing.arm()
    screen = QGuiApplication.primaryScreen()
    first = Step(QPointF(5, 5), 1, QColor("blue"))
    second = Step(QPointF(15, 8), 2, QColor("blue"))
    drawing.drawn.emit(screen, (first,))
    assert drawing.pending and not drawing.busy
    assert 2.8 < drawing.countdown <= 3
    assert h.runtime._auto_timer.interval() == 3000
    assert not h.ui.snaps

    drawing.interacting = True
    drawing.interaction_changed.emit(True)
    assert not h.runtime._auto_timer.isActive()
    assert drawing.countdown == 0
    # A queued timeout cannot take a partial stroke.
    h.runtime._auto_timer.timeout.emit()
    assert not h.ui.snaps
    drawing.interacting = False
    drawing.interaction_changed.emit(False)
    drawing.drawn.emit(screen, (first, second))
    assert h.runtime._auto_timer.isActive()
    assert not drawing.busy
    assert h.runtime._auto_draw_pending == (screen, (first, second))

    h.recorder.get_recording_duration = lambda: 16.0
    h.runtime._auto_timer.timeout.emit()
    (snap,) = h.ui.snaps
    assert drawing.busy and drawing.pending and drawing.countdown == 0
    snap.keep()
    (shot,) = auto.session.saved_shots()
    assert shot.kept_at_s == 16.0  # Words close at capture, not at the first mark.
    assert not drawing.pending and not drawing.busy
    assert drawing.captured


def test_failed_delayed_drawing_does_not_clear_its_marks(h, auto):
    h.settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] = 3
    drawing = h.ui.drawing
    drawing.drawn.emit(QGuiApplication.primaryScreen(), ())
    h.runtime._auto_timer.timeout.emit()
    (snap,) = h.ui.snaps
    snap.fail("Capture failed")
    assert not drawing.pending and not drawing.busy
    assert not drawing.captured
    assert auto.session.shot_count == 0


def test_capture_delay_changes_apply_to_pending_clicks_and_drawings(h, auto):
    h.settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] = 5
    drawing = h.ui.drawing
    auto.click(20, 40)
    assert h.runtime._auto_timer.interval() == 5000
    assert 4.8 < drawing.countdown <= 5
    h.settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] = 1
    drawing.capture_delay_changed.emit(1)
    assert h.runtime._auto_timer.interval() == 1000
    assert 0.8 < drawing.countdown <= 1
    auto.click(50, 60)
    assert h.runtime._auto_position == (50, 60)
    h.runtime._auto_timer.timeout.emit()
    (snap,) = h.ui.snaps
    assert snap.click_at == (50, 60)
    snap.keep()

    drawing.drawn.emit(QGuiApplication.primaryScreen(), ())
    assert drawing.pending
    h.settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] = 0
    drawing.capture_delay_changed.emit(0)
    assert not h.runtime._auto_progress.isActive()
    assert drawing.countdown == 0
    h.runtime._auto_timer.timeout.emit()
    assert len(h.ui.snaps) == 2


@pytest.mark.parametrize("cancel", ["escape", "clear", "pause", "end"])
def test_pending_delayed_drawing_is_cancelled_without_a_stray_capture(h, auto, cancel):
    h.settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] = 3
    drawing = h.ui.drawing
    drawing.drawn.emit(QGuiApplication.primaryScreen(), ())
    if cancel in ("escape", "clear"):
        drawing.capture_cancelled.emit()
    elif cancel == "pause":
        h.recorder.pause = lambda: setattr(h.recorder, "paused", True) or True
        h.runtime.set_paused(True)
    else:
        h.controller.stop_recording()
    assert not h.runtime._auto_timer.isActive()
    assert not h.runtime._auto_progress.isActive()
    assert not drawing.pending and drawing.countdown == 0
    h.runtime._auto_timer.timeout.emit()
    assert not h.ui.snaps


@pytest.mark.parametrize("mid_stroke", [False, True])
def test_done_keeps_pending_drawing_before_finishing(h, auto, mid_stroke):
    h.settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] = 5
    drawing = h.ui.drawing
    screen = QGuiApplication.primaryScreen()
    drawing.drawn.emit(screen, ())
    drawing.interacting = mid_stroke
    if mid_stroke:
        drawing.interaction_changed.emit(True)
    h.runtime.finish()
    h.controller.stop_recording.assert_not_called()
    if mid_stroke:
        assert not drawing.busy
        drawing.interacting = False
        drawing.interaction_changed.emit(False)
        drawing.drawn.emit(screen, ())
    (snap,) = h.ui.snaps
    snap.keep()
    assert auto.session.shot_count == 1
    h.controller.stop_recording.assert_called_once()


def test_auto_mode_drops_an_in_flight_shot_on_pause_or_end(h, auto):
    def pause():
        h.recorder.paused = True
        return True

    h.recorder.pause = pause
    h.recorder.resume = lambda: setattr(h.recorder, "paused", False)
    auto.click(20, 40)
    h.runtime._auto_timer.timeout.emit()
    (first,) = h.ui.snaps
    h.runtime.set_paused(True)
    assert not h.ui.drawing.enabled
    auto.click(22, 42)
    assert first.drops == 1
    assert not h.runtime._auto_timer.isActive()

    h.runtime.set_paused(False)
    assert h.ui.drawing.enabled
    auto.click(20, 40)
    h.runtime._auto_timer.timeout.emit()
    _, second = h.ui.snaps
    h.controller.stop_recording()
    assert second.drops == 1
    assert auto.session.shot_count == 0
    assert not h.ui.copied
    auto.hook.stop.assert_called_once()


def test_mac_auto_mode_requests_screen_recording_before_watching_clicks(h, monkeypatch):
    h.settings.values[SettingsKey.WALKTHROUGH_AUTO_MODE] = True
    monkeypatch.setattr(walkthrough_module, "sys", SimpleNamespace(platform="darwin"))
    monitor = Mock()
    monkeypatch.setattr(walkthrough_module, "MouseClickMonitor", monitor)
    h.ui.supported = False, "Screen Recording is required"
    session = _begin(h)
    try:
        monitor.assert_not_called()
        assert h.ui.access_requests == 1
        assert (h.ui.supported[1], False) in h.ui.notices
        assert not h.ui.walkthrough_panel.auto_mode
        assert h.ui.drawing is None
        assert h.recorder.is_recording
    finally:
        session.end()


@pytest.mark.parametrize("failure", ["monitor", "drawing"])
def test_mac_auto_startup_failure_leaves_manual_snap_available(h, monkeypatch, failure):
    h.settings.values[SettingsKey.WALKTHROUGH_AUTO_MODE] = True
    monkeypatch.setattr(walkthrough_module, "sys", SimpleNamespace(platform="darwin"))
    hook = Mock()
    hook.wait_until_installed.return_value = failure != "monitor"
    monkeypatch.setattr(walkthrough_module, "MouseClickMonitor", Mock(return_value=hook))
    if failure == "drawing":
        monkeypatch.setattr(h.ui, "start_walkthrough_drawing", Mock(side_effect=RuntimeError("drawing unavailable")))
    session = _begin(h)
    try:
        hook.stop.assert_called_once()
        assert h.runtime._auto_hook is None
        assert not h.ui.walkthrough_panel.auto_mode
        assert h.ui.drawing is None
        h.runtime.snap()
        h.ui.snaps[0].keep()
        assert session.shot_count == 1
        assert h.recorder.is_recording
    finally:
        h.runtime.cleanup()
        session.end()


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


def test_done_does_not_wait_out_a_capture_delay_for_a_pending_click(h, auto):
    h.settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] = 10
    auto.click(20, 40)
    assert h.runtime._auto_timer.interval() == 10000

    h.runtime.finish()

    # The click is still captured, after the usual settle, not ten seconds on.
    assert h.runtime._auto_timer.isActive()
    assert h.runtime._auto_timer.interval() == walkthrough_module.AUTO_SETTLE_MS
    assert h.ui.drawing.countdown == 0
    h.runtime._auto_timer.timeout.emit()
    (snap,) = h.ui.snaps
    snap.keep()
    h.controller.stop_recording.assert_called_once()


def test_canceling_drops_the_screenshot_before_the_recorder_says_canceled(h):
    _begin(h)
    h.runtime.snap()
    # As TranscriptionRuntime._cancel_recording announces the stop: the
    # recorder is only told to cancel afterwards.
    h.transcription.canceling_recording = True
    h.transcription.recording_walkthrough = None
    h.runtime.on_recording_state_changed(False)

    assert h.ui.snaps[0].drops == 1
    assert h.ui.copied == []


def test_a_snap_menu_pick_during_the_countdown_takes_that_kind(h):
    _begin(h)
    h.settings.values[SettingsKey.WALKTHROUGH_SNAP_MODE] = WalkthroughSnapMode.DELAYED
    h.runtime.snap("ui")
    assert h.runtime._countdown_left and not h.ui.snaps

    h.runtime.choose_snap_mode(WalkthroughSnapMode.SCREEN)

    assert not h.runtime._countdown_left
    (snap,) = h.ui.snaps
    assert snap.take == WalkthroughSnapMode.SCREEN


def test_a_delayed_drawing_on_another_monitor_gets_its_own_screenshot(h, auto):
    h.settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] = 3
    drawing = h.ui.drawing
    left, right = QGuiApplication.primaryScreen(), object()
    drawing.drawn.emit(left, ())
    assert h.runtime._auto_draw_pending == (left, ())

    drawing.drawn.emit(right, ())

    # The first monitor's drawing is taken now, not replaced by the second's.
    (first,) = h.ui.snaps
    assert first.screen is left
    first.keep()
    assert drawing.captured_screen is left
    # The second then waits out its own countdown.
    assert h.runtime._auto_draw_pending == (right, ())
    assert drawing.pending and h.runtime._auto_timer.isActive()
    h.runtime._auto_timer.timeout.emit()
    assert h.ui.snaps[1].screen is right
    h.ui.snaps[1].keep()
    assert len(auto.session.saved_shots()) == 2
    assert not drawing.pending and h.runtime._auto_draw_next is None
