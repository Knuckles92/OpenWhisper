"""Command Mode and transforms on Wayland never lose the user's clipboard.

A Wayland compositor sends the clipboard only to the focused window. Command
Mode and transforms copy the selection while another app has focus, so Qt
must read the clipboard through wlr-data-control, which it uses only when
asked. Where it cannot see other apps' copies, no copy is sent at all and
the user is told the selection can't be read here.

Offscreen only: every clipboard here is a stand-in, never the system one.
"""

import os
import time
from types import SimpleNamespace

import pytest

from PyQt6.QtCore import QByteArray, QCoreApplication, QEventLoop, QMimeData, QObject, QTimer, pyqtSignal

from services import focus_context, synthetic_keys, text_rewrite
from services.focus_context import FocusSnapshot
from services.runtime import command
from services.runtime.command import CommandRefused, CommandRuntime
from services.settings import RecordingTriggerMode, SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_personalize_s3_command_runtime import (
    NOTEPAD,
    FakeRuntime,
    FakeService,
    FakeUI,
    Signal,
    _settle,
)
from ui_qt import clipboard as clipboard_module
from ui_qt.clipboard import TemporaryClipboard

DATA_CONTROL = "QT_WAYLAND_USE_DATA_CONTROL"
HIDDEN = "OpenWhisper can't read selected text on this desktop"
URL = "https://example.com/the-url-i-copied"
PARAGRAPH = "Selected paragraph to make formal."


@pytest.fixture(autouse=True)
def qt_app(_session_qt_application):
    return _session_qt_application


@pytest.fixture(autouse=True)
def no_data_control_variable(monkeypatch):
    # Set first so the variable a test leaves behind is removed afterwards.
    monkeypatch.setenv(DATA_CONTROL, "")
    monkeypatch.delenv(DATA_CONTROL)


def _wait_for(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(0.002)
    return predicate()


class Compositor:
    """The seat's real clipboard, which only some clients are shown."""

    def __init__(self, text):
        self.text = text


class UnfocusedWaylandClipboard(QObject):
    """Qt's view of the clipboard on Wayland without data-control, while unfocused.

    Another app's copy never reaches it, and the offer Qt still holds is
    dead: its formats are listed but every one reads back empty.
    """

    dataChanged = pyqtSignal()

    def __init__(self, compositor):
        super().__init__()
        self.compositor = compositor
        self.reads = 0
        self.writes = []

    def mimeData(self):
        self.reads += 1
        mime = QMimeData()
        for mime_format in ("text/plain", "text/html"):
            mime.setData(mime_format, QByteArray(b""))
        return mime

    def setMimeData(self, mime):
        self.writes.append(mime.text())
        self.compositor.text = mime.text()

    def text(self):
        return ""


class DataControlClipboard(QObject):
    """Qt's view through wlr-data-control: every copy is seen and signalled."""

    dataChanged = pyqtSignal()

    def __init__(self, compositor):
        super().__init__()
        self.compositor = compositor
        self.writes = []

    def mimeData(self):
        mime = QMimeData()
        mime.setText(self.compositor.text)
        return mime

    def setMimeData(self, mime):
        self.writes.append(mime.text())
        self.compositor.text = mime.text()
        self.dataChanged.emit()

    def text(self):
        return self.compositor.text

    def copied_by_other_app(self, text):
        self.compositor.text = text
        self.dataChanged.emit()


class TargetApp(QObject):
    """The focused app: Ctrl+C puts its selection on the seat 30 ms later."""

    def __init__(self, compositor, selected, on_copy=None):
        super().__init__()
        self.compositor, self.selected, self.on_copy = compositor, selected, on_copy
        self.copies = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._land)

    def __call__(self):
        self.copies += 1
        self._timer.start()

    def _land(self):
        if self.on_copy is not None:
            self.on_copy(self.selected)
        else:
            self.compositor.text = self.selected


def _temporary(clipboard, **kwargs):
    temporary = TemporaryClipboard(clipboard, sequence_source=lambda: None, **kwargs)
    # Linux has no change counter; the constructor would read the real OS one.
    temporary._sequence = None
    temporary._render_text = temporary._render_html = None
    return temporary


def _capture(temporary, copy):
    results = []
    temporary.capture_selection(send_copy=copy, callback=results.append, timeout_ms=700)
    assert _wait_for(lambda: results)
    return results[0]


def test_a_clipboard_that_cant_see_other_apps_sends_no_copy_and_writes_nothing():
    seat = Compositor(URL)
    clipboard = UnfocusedWaylandClipboard(seat)
    target = TargetApp(seat, PARAGRAPH)

    assert _capture(_temporary(clipboard, sees_foreign_copies=False), target) is None
    assert target.copies == 0
    assert clipboard.writes == [] and clipboard.reads == 0
    assert seat.text == URL


def test_through_data_control_the_copy_is_read_and_the_url_goes_back():
    seat = Compositor(URL)
    clipboard = DataControlClipboard(seat)
    target = TargetApp(seat, PARAGRAPH, on_copy=clipboard.copied_by_other_app)

    assert _capture(_temporary(clipboard, sees_foreign_copies=True), target) == PARAGRAPH
    assert target.copies == 1
    assert seat.text == URL


def test_the_users_own_text_handed_over_again_is_not_taken_for_the_selection():
    # A clipboard manager such as wl-clip-persist takes over each new
    # clipboard, so Qt can hear a change that still holds the user's text
    # before the copy lands.
    seat = Compositor(URL)
    clipboard = DataControlClipboard(seat)
    target = TargetApp(seat, PARAGRAPH, on_copy=clipboard.copied_by_other_app)

    def copy():
        clipboard.copied_by_other_app(URL)
        target()

    assert _capture(_temporary(clipboard, sees_foreign_copies=True), copy) == PARAGRAPH
    assert seat.text == URL


def test_a_selection_equal_to_the_clipboard_still_arrives_by_the_deadline():
    seat = Compositor(PARAGRAPH)
    clipboard = DataControlClipboard(seat)
    target = TargetApp(seat, PARAGRAPH, on_copy=clipboard.copied_by_other_app)

    assert _capture(_temporary(clipboard, sees_foreign_copies=True), target) == PARAGRAPH
    assert seat.text == PARAGRAPH


def _platform(monkeypatch, name, qt_version="6.11.2"):
    monkeypatch.setattr(clipboard_module, "QGuiApplication",
                        SimpleNamespace(platformName=lambda: name))
    monkeypatch.setattr(clipboard_module, "qVersion", lambda: qt_version)


@pytest.mark.parametrize(("platform", "value", "qt_version", "visible"), [
    ("wayland", "1", "6.11.2", True),
    ("wayland-egl", "1", "6.10.0", True),
    ("wayland", None, "6.11.2", False),
    ("wayland", "0", "6.11.2", False),
    ("wayland", "yes", "6.11.2", False),
    ("wayland", "1", "6.8.3", False),
    ("xcb", None, "6.11.2", True),
    ("windows", None, "6.11.2", True),
    ("cocoa", None, "6.11.2", True),
    ("offscreen", None, "6.11.2", True),
])
def test_only_wayland_without_data_control_hides_other_apps_copies(
    monkeypatch, platform, value, qt_version, visible,
):
    _platform(monkeypatch, platform, qt_version)
    if value is not None:
        monkeypatch.setenv(DATA_CONTROL, value)

    assert clipboard_module.foreign_copies_visible() is visible


def test_the_clipboard_checks_the_desktop_itself(monkeypatch):
    _platform(monkeypatch, "wayland")
    monkeypatch.setenv(DATA_CONTROL, "0")
    seat = Compositor(URL)
    clipboard = UnfocusedWaylandClipboard(seat)
    target = TargetApp(seat, PARAGRAPH)

    assert _capture(_temporary(clipboard), target) is None
    assert target.copies == 0 and clipboard.writes == []


def test_linux_asks_qt_for_data_control(monkeypatch):
    monkeypatch.setattr(clipboard_module.sys, "platform", "linux")

    clipboard_module.prefer_wayland_data_control()

    assert os.environ[DATA_CONTROL] == "1"


def test_a_users_own_choice_is_kept(monkeypatch):
    monkeypatch.setattr(clipboard_module.sys, "platform", "linux")
    monkeypatch.setenv(DATA_CONTROL, "0")

    clipboard_module.prefer_wayland_data_control()

    assert os.environ[DATA_CONTROL] == "0"


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_other_systems_are_left_alone(monkeypatch, platform):
    monkeypatch.setattr(clipboard_module.sys, "platform", platform)

    clipboard_module.prefer_wayland_data_control()

    assert DATA_CONTROL not in os.environ


def test_the_app_asks_before_qt_starts(monkeypatch):
    import ui_qt.app as app_module

    seen = []

    class Stop(Exception):
        pass

    class StubApplication:
        @staticmethod
        def instance():
            return None

        def __init__(self, argv):
            # Qt's Wayland client reads the variable once, while starting.
            seen.append(os.environ.get(DATA_CONTROL))
            raise Stop

    for name in ("setApplicationName", "setApplicationDisplayName", "setOrganizationName",
                 "setDesktopFileName", "setStyle"):
        setattr(StubApplication, name, staticmethod(lambda *args: None))
    monkeypatch.setattr(app_module, "QApplication", StubApplication)
    monkeypatch.setattr(app_module, "use_omarchy_ui", lambda: False)
    monkeypatch.setattr(clipboard_module.sys, "platform", "linux")

    with pytest.raises(Stop):
        app_module.QtApplication()

    assert seen == ["1"]


# --- what Command Mode and transforms do with it -------------------------------


@pytest.fixture
def h(monkeypatch):
    settings = InMemorySettings({
        SettingsKey.RECORDING_TRIGGER_MODE: RecordingTriggerMode.PUSH_HOLD,
    })
    monkeypatch.setattr(command, "settings_manager", settings)
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: True)
    monkeypatch.setattr(synthetic_keys, "wait_for_modifiers_released", lambda timeout_s=0.8: True)
    focus_context.set_service(FakeService(FocusSnapshot(NOTEPAD, None)))
    recorder = SimpleNamespace(is_recording=False)
    controller = SimpleNamespace(
        recorder=recorder,
        status_update=Signal(),
        overlay_state_update=Signal(),
        stop_recording=lambda: setattr(recorder, "is_recording", False),
        cancel=lambda: setattr(recorder, "is_recording", False),
        ui_controller=FakeUI(),
    )
    runtime = FakeRuntime(controller, settings)
    controller.transcription_runtime = runtime
    commands = CommandRuntime(controller)
    # What capture_selection hands back where Qt can't see the copy.
    controller.ui_controller.clipboard_selection = None
    yield SimpleNamespace(controller=controller, runtime=runtime, commands=commands)
    commands.cleanup()


def test_command_mode_says_so_instead_of_writing_over_the_selection(h):
    h.commands.key_pressed(1.0)
    job = h.runtime._job
    h.commands.key_released(2.0)
    _settle(job.selection.done)

    with pytest.raises(CommandRefused, match=f"^{HIDDEN}$"):
        h.commands.complete_recording("make this more formal", job)
    assert h.runtime._transcript_cleanup.calls == []


def test_a_transform_says_so(h):
    h.commands.run_transform("polish")

    _settle(lambda: h.runtime.events[-1][0] == "abandon")
    assert h.runtime.events[-1] == ("abandon", HIDDEN)


def test_a_transform_end_to_end_keeps_the_users_clipboard(h):
    seat = Compositor(URL)
    clipboard = UnfocusedWaylandClipboard(seat)
    target = TargetApp(seat, PARAGRAPH)
    temporary = _temporary(clipboard, sees_foreign_copies=False)
    h.controller.ui_controller = SimpleNamespace(
        capture_selection=lambda callback, timeout_ms=None: temporary.capture_selection(
            send_copy=target, callback=callback, timeout_ms=700),
    )

    h.commands.run_transform("polish")

    _settle(lambda: h.runtime.events[-1][0] == "abandon", timeout=3.0)
    assert h.runtime.events[-1] == ("abandon", HIDDEN)
    assert target.copies == 0 and clipboard.writes == []
    assert seat.text == URL

