"""start_snap, SnapSession and capture; Windows affinity, platform support, grim.

No test here grabs the real screen: every capture goes through the
``capture._grab_screen`` seam, which an autouse fixture replaces.
"""

import gc
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from PyQt6 import sip
from PyQt6.QtCore import (
    QBuffer,
    QCoreApplication,
    QEvent,
    QIODevice,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QSize,
    Qt,
)
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from ui_qt.snap import affinity, capture, grim, session, support
from ui_qt.snap import start_snap as public_start_snap
from ui_qt.snap.session import CAPTURE_FAILED, start_snap

REAL_GRAB = capture._grab_screen


def _screen_image(screen, scale: float = 1.0) -> QImage:
    size = screen.geometry().size() * scale
    image = QImage(size, QImage.Format.Format_RGB32)
    image.fill(QColor("#336699"))
    return image


@pytest.fixture(autouse=True)
def grabs(monkeypatch):
    """Fake captures that record what was on screen, on a supported desktop."""
    calls = []

    def fake(screen):
        calls.append(
            {
                "screen": screen,
                "visible": [w for w in QApplication.topLevelWidgets() if w.isVisible()],
            }
        )
        return _screen_image(screen)

    monkeypatch.setattr(capture, "_grab_screen", fake)
    monkeypatch.setattr(session, "snap_support", lambda: (True, ""))
    yield calls
    for live in list(session._live):
        if not sip.isdeleted(live):
            live.drop()
    # A failure still waiting to be reported belongs to its own test.
    session._live.clear()


class Spy:
    def __init__(self, signal):
        self.calls = []
        signal.connect(lambda *args: self.calls.append(args))

    def __len__(self):
        return len(self.calls)


def _pump_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    return bool(predicate())


def _pump(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)


@pytest.fixture
def window():
    widget = QWidget()
    widget.setWindowTitle("Recording HUD")
    widget.resize(120, 40)
    widget.show()
    yield widget
    widget.hide()
    widget.deleteLater()


class TestOutcomes:
    def test_a_failed_capture_is_reported_after_start_snap_returns(self, monkeypatch):
        monkeypatch.setattr(capture, "_grab_screen", lambda screen: None)

        snap = start_snap()
        failed = Spy(snap.failed)

        assert len(failed) == 0
        assert not snap.active
        assert _pump_until(lambda: len(failed) == 1)
        _pump(0.05)
        assert failed.calls == [(CAPTURE_FAILED,)]

    def test_a_capture_that_raises_is_a_failure(self, monkeypatch):
        def broken(screen):
            raise OSError("no display")

        monkeypatch.setattr(capture, "_grab_screen", broken)

        snap = start_snap()
        failed = Spy(snap.failed)

        assert _pump_until(lambda: len(failed) == 1)
        assert failed.calls == [(CAPTURE_FAILED,)]

    def test_an_unsupported_desktop_fails_with_its_reason_without_capturing(
        self, monkeypatch, grabs
    ):
        monkeypatch.setattr(
            session, "snap_support", lambda: (False, "Not on this desktop.")
        )

        snap = start_snap()
        failed = Spy(snap.failed)

        assert len(failed) == 0
        assert _pump_until(lambda: len(failed) == 1)
        assert failed.calls == [("Not on this desktop.",)]
        assert grabs == []

    def test_the_overlay_opens_on_the_grabbed_screen(self, grabs):
        snap = start_snap(hint="Snap 2")

        overlay = snap._overlay
        assert overlay is not None and overlay.isVisible()
        assert snap.active
        assert grabs[0]["screen"] is overlay._screen
        assert overlay.windowTitle() == "OpenWhisper Snap"

    def test_keep_emits_the_whole_screen_once_and_deletes_the_overlay(self):
        snap = start_snap()
        kept, dropped = Spy(snap.kept), Spy(snap.dropped)
        overlay = snap._overlay
        screen = overlay._screen

        snap.keep()
        snap.keep()
        snap.drop()

        assert [image.size() for (image,) in kept.calls] == [screen.geometry().size()]
        assert len(dropped) == 0
        assert not snap.active
        assert overlay.isHidden()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert sip.isdeleted(overlay)

    def test_drop_emits_once(self):
        snap = start_snap()
        kept, dropped = Spy(snap.kept), Spy(snap.dropped)

        snap.drop()
        snap.drop()
        snap.keep()

        assert (len(dropped), len(kept)) == (1, 0)

    def test_keys_in_the_overlay_reach_the_session(self):
        snap = start_snap()
        kept = Spy(snap.kept)

        QTest.keyClick(snap._overlay, Qt.Key.Key_Return)

        assert len(kept) == 1
        assert not snap.active

    def test_a_session_nobody_holds_still_reports(self):
        kept = []
        start_snap().kept.connect(kept.append)

        gc.collect()
        (live,) = [snap for snap in session._live if snap.active]
        live.keep()

        assert len(kept) == 1
        assert live not in session._live

    def test_an_owner_deleted_mid_snap_takes_the_overlay_with_it(self):
        owner = QObject()
        snap = start_snap(parent=owner)
        overlay = snap._overlay

        sip.delete(owner)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        assert sip.isdeleted(overlay)

    def test_start_snap_refuses_other_threads(self):
        errors = []

        def worker():
            try:
                start_snap()
            except RuntimeError as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert len(errors) == 1

    def test_the_package_exports_the_public_api(self):
        import ui_qt.snap as snap

        assert public_start_snap is start_snap
        assert set(snap.__all__) == {
            "SnapSession",
            "keep_out_of_screenshots",
            "snap_support",
            "start_snap",
        }


class TestKeepOut:
    def test_windows_are_hidden_for_the_grab_then_restored(self, window, grabs):
        snap = start_snap(keep_out=[window])

        # Hidden now, captured after a couple of frames, never inside start_snap.
        assert not window.isVisible()
        assert grabs == []
        assert _pump_until(lambda: snap._overlay is not None)
        assert window not in grabs[0]["visible"]
        assert window.isVisible()
        snap.drop()

    def test_windows_kept_out_by_affinity_stay_on_screen(
        self, monkeypatch, window, grabs
    ):
        monkeypatch.setattr(support, "_platform", lambda: "win32")
        monkeypatch.setattr(affinity, "_user32", lambda: FakeUser32())
        assert affinity.keep_out_of_screenshots(window, True)

        snap = start_snap(keep_out=[window])

        assert len(grabs) == 1
        assert window in grabs[0]["visible"]
        assert window.isVisible()
        snap.drop()

    def test_hidden_and_minimised_windows_are_left_alone(self, window, grabs):
        hidden = QWidget()
        window.showMinimized()

        snap = start_snap(keep_out=[hidden, window, None])

        assert len(grabs) == 1
        assert not hidden.isVisible()
        snap.drop()
        hidden.deleteLater()

    def test_drop_while_waiting_restores_windows_without_capturing(self, window, grabs):
        snap = start_snap(keep_out=[window])
        dropped = Spy(snap.dropped)

        snap.drop()

        assert len(dropped) == 1
        assert window.isVisible()
        _pump(0.25)
        assert grabs == []

    def test_keep_while_waiting_keeps_the_whole_screen_once_captured(self, window):
        snap = start_snap(keep_out=[window])
        kept = Spy(snap.kept)

        snap.keep()

        assert len(kept) == 0
        assert _pump_until(lambda: len(kept) == 1)
        assert snap._overlay is None
        (image,) = kept.calls[0]
        assert image.size() == QGuiApplication.primaryScreen().geometry().size()
        assert window.isVisible()

    def test_an_owner_deleted_while_waiting_shows_the_windows_again(
        self, window, grabs
    ):
        owner = QObject()
        start_snap(keep_out=[window], parent=owner)
        assert not window.isVisible()

        sip.delete(owner)

        assert window.isVisible()
        _pump(0.25)
        assert grabs == []

    def test_one_window_listed_twice_is_hidden_once(self, window):
        hidden = capture.hide_windows([window, window])

        assert hidden == [window]
        capture.restore_windows(hidden)
        assert window.isVisible()


class Parked:
    """A float_above window: logs park and unpark with what was on screen."""

    def __init__(self, log):
        self.log = log

    def park(self, area):
        self.log.append(("park", area))

    def unpark(self):
        self.log.append(("unpark",))


class TestFloatAbove:
    def test_windows_park_over_the_open_overlay_and_return_before_the_outcome(self):
        log = []
        snap = start_snap(float_above=[Parked(log)])
        snap.kept.connect(lambda image: log.append(("kept",)))

        assert log == [("park", snap._overlay._screen.geometry())]
        snap.keep()

        assert log[1:] == [("unpark",), ("kept",)]

    def test_a_snap_that_never_opens_parks_nothing(self, monkeypatch):
        monkeypatch.setattr(capture, "_grab_screen", lambda screen: None)
        log = []

        snap = start_snap(float_above=[Parked(log)])

        assert _pump_until(lambda: not session._live)
        assert not snap.active
        assert log == []

    def test_an_owner_deleted_mid_snap_puts_the_windows_back(self):
        log = []
        owner = QObject()
        start_snap(float_above=[Parked(log)], parent=owner)

        sip.delete(owner)

        assert [entry[0] for entry in log] == ["park", "unpark"]


def _drag_on(widget, start: QPoint, end: QPoint) -> None:
    for kind, point, held in (
        (QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseMove, end, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton),
    ):
        button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
        QApplication.sendEvent(widget, QMouseEvent(
            kind, QPointF(point), QPointF(widget.mapToGlobal(point)), button, held,
            Qt.KeyboardModifier.NoModifier,
        ))


class TestEveryScreen:
    @pytest.fixture
    def two_screens(self, monkeypatch):
        """The test desktop has one screen: cover it twice, as two monitors would be."""
        screen = QGuiApplication.primaryScreen()
        monkeypatch.setattr(capture, "screens_to_cover", lambda: [screen, screen])
        return screen

    def test_each_screen_is_captured_and_covered(self, two_screens, grabs):
        snap = start_snap()

        assert [call["screen"] for call in grabs] == [two_screens, two_screens]
        assert len(snap._overlays) == 2
        assert all(overlay.isVisible() for overlay in snap._overlays)

    def test_selecting_on_another_screen_moves_the_selection_there(self, two_screens):
        snap = start_snap()
        kept = Spy(snap.kept)
        first, second = snap._overlays
        first._select_all()

        _drag_on(second, QPoint(10, 10), QPoint(110, 60))

        assert first.selection is None
        assert second.selection == QRect(10, 10, 100, 50)
        assert snap._overlay is second
        snap.keep()
        (image,) = kept.calls[0]
        assert image.size() == QSize(100, 50)
        assert not any(overlay.isVisible() for overlay in (first, second))

    def test_esc_on_any_screen_drops_the_whole_snap(self, two_screens):
        snap = start_snap()
        dropped = Spy(snap.dropped)
        overlays = list(snap._overlays)

        QTest.keyClick(overlays[1], Qt.Key.Key_Escape)

        assert len(dropped) == 1
        assert not any(overlay.isVisible() for overlay in overlays)

    def test_a_screen_that_fails_to_capture_is_left_uncovered(self, monkeypatch, two_screens):
        results = iter([_screen_image(two_screens), None])
        monkeypatch.setattr(capture, "_grab_screen", lambda screen: next(results))

        snap = start_snap()

        assert len(snap._overlays) == 1
        snap.drop()

    def test_the_pointers_screen_comes_first_and_hyprland_covers_only_it(self, monkeypatch):
        a, b, c = object(), object(), object()
        monkeypatch.setattr(capture, "screen_under_cursor", lambda: b)
        monkeypatch.setattr(capture, "QGuiApplication", SimpleNamespace(screens=lambda: [a, b, c]))
        monkeypatch.setattr(support, "uses_grim", lambda: False)
        assert capture.screens_to_cover() == [b, a, c]

        monkeypatch.setattr(support, "uses_grim", lambda: True)
        assert capture.screens_to_cover() == [b]


class FakeUser32:
    def __init__(self, result: int = 1):
        self.result = result
        self.calls = []

    def SetWindowDisplayAffinity(self, hwnd, affinity_value):
        self.calls.append((hwnd, affinity_value))
        return self.result


class TestAffinity:
    @pytest.fixture
    def user32(self, monkeypatch):
        fake = FakeUser32()
        monkeypatch.setattr(support, "_platform", lambda: "win32")
        monkeypatch.setattr(affinity, "_user32", lambda: fake)
        return fake

    def test_excludes_then_restores_the_window(self, user32, window):
        hwnd = int(window.winId())

        assert affinity.keep_out_of_screenshots(window, True) is True
        assert affinity.is_kept_out(window)
        assert affinity.keep_out_of_screenshots(window, False) is True
        assert not affinity.is_kept_out(window)

        assert user32.calls == [(hwnd, 0x11), (hwnd, 0x0)]

    def test_a_refused_exclusion_is_not_remembered(self, user32, window):
        user32.result = 0

        assert affinity.keep_out_of_screenshots(window, True) is False
        assert not affinity.is_kept_out(window)

    def test_a_recreated_native_window_counts_as_not_excluded(self, user32, window):
        affinity.keep_out_of_screenshots(window, True)
        window.setProperty(affinity._EXCLUDED_HWND, int(window.winId()) + 1)

        assert not affinity.is_kept_out(window)

    def test_a_child_widget_means_its_window(self, user32, window):
        child = QWidget(window)

        affinity.keep_out_of_screenshots(child, True)

        assert user32.calls == [(int(window.winId()), 0x11)]
        assert affinity.is_kept_out(window)

    def test_macos_shares_the_window_as_none(self, monkeypatch, window):
        shared = []
        monkeypatch.setattr(support, "_platform", lambda: "darwin")
        monkeypatch.setattr(
            affinity.macos, "set_shared", lambda w, on: shared.append((w, on)) or True
        )

        assert affinity.keep_out_of_screenshots(window, True) is True
        assert affinity.is_kept_out(window)
        assert affinity.keep_out_of_screenshots(window, False) is True
        assert not affinity.is_kept_out(window)

        assert shared == [(window, False), (window, True)]

    def test_a_macos_window_left_shared_is_not_remembered(self, monkeypatch, window):
        monkeypatch.setattr(support, "_platform", lambda: "darwin")
        monkeypatch.setattr(affinity.macos, "set_shared", lambda w, on: False)

        assert affinity.keep_out_of_screenshots(window, True) is False
        assert not affinity.is_kept_out(window)

    def test_elsewhere_it_does_nothing(self, monkeypatch, window):
        fake = FakeUser32()
        monkeypatch.setattr(support, "_platform", lambda: "linux")
        monkeypatch.setattr(affinity, "_user32", lambda: fake)

        assert affinity.keep_out_of_screenshots(window, True) is False
        assert fake.calls == []
        assert not affinity.is_kept_out(window)


class TestSupport:
    @pytest.fixture
    def desktop(self, monkeypatch):
        def pose(platform, *, hyprland=False, grim_path=None, wayland=False):
            monkeypatch.setattr(support, "_platform", lambda: platform)
            monkeypatch.setattr(support.hyprland, "available", lambda: hyprland)
            monkeypatch.setattr(
                support.shutil,
                "which",
                lambda name: grim_path if name == "grim" else None,
            )
            monkeypatch.setattr(support, "is_wayland", lambda: wayland)
            monkeypatch.setattr(support, "is_wayland_session", lambda: wayland)

        return pose

    def test_windows_is_supported(self, desktop):
        desktop("win32")

        assert support.snap_support() == (True, "")

    def test_x11_is_supported(self, desktop):
        desktop("linux")

        assert support.snap_support() == (True, "")

    def test_hyprland_with_grim_is_supported(self, desktop):
        desktop("linux", hyprland=True, grim_path="/usr/bin/grim", wayland=True)

        assert support.snap_support() == (True, "")
        assert support.uses_grim()

    def test_hyprland_without_grim_says_to_install_it(self, desktop):
        desktop("linux", hyprland=True, wayland=True)

        supported, reason = support.snap_support()

        assert not supported
        assert "grim" in reason

    def test_other_wayland_desktops_are_not_supported_yet(self, desktop):
        desktop("linux", wayland=True)

        supported, reason = support.snap_support()

        assert not supported
        assert "Wayland" in reason
        assert not support.uses_grim()

    def test_other_systems_are_not_supported_yet(self, desktop):
        desktop("freebsd14")

        supported, reason = support.snap_support()

        assert not supported
        assert "system" in reason

    def test_macos_with_screen_recording_is_supported(self, desktop, monkeypatch):
        desktop("darwin")
        monkeypatch.setattr(support.macos, "capture_granted", lambda: True)

        assert support.snap_support() == (True, "")

    def test_macos_without_screen_recording_says_where_to_grant_it(
        self, desktop, monkeypatch
    ):
        desktop("darwin")
        monkeypatch.setattr(support.macos, "capture_granted", lambda: False)

        supported, reason = support.snap_support()

        assert not supported
        assert "Screen Recording" in reason


class TestRequestAccess:
    @pytest.fixture
    def mac(self, monkeypatch):
        calls = []
        monkeypatch.setattr(support, "_platform", lambda: "darwin")
        monkeypatch.setattr(support, "_asked", False)
        monkeypatch.setattr(support.macos, "capture_granted", lambda: False)
        monkeypatch.setattr(
            support.macos, "request_capture", lambda: calls.append("request") or False
        )
        monkeypatch.setattr(
            support.macos, "open_capture_settings", lambda: calls.append("settings")
        )
        return calls

    def test_asks_macos_once_then_opens_settings(self, mac):
        support.request_access()
        support.request_access()
        support.request_access()

        assert mac == ["request", "settings", "settings"]

    def test_nothing_to_ask_once_granted(self, mac, monkeypatch):
        monkeypatch.setattr(support.macos, "capture_granted", lambda: True)

        support.request_access()

        assert mac == []

    def test_nothing_to_ask_elsewhere(self, mac, monkeypatch):
        monkeypatch.setattr(support, "_platform", lambda: "linux")

        support.request_access()

        assert mac == []


def _png(width: int, height: int) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#ff4f3a"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def _fake_screen(name: str, rect: QRect):
    return SimpleNamespace(name=lambda: name, geometry=lambda: QRect(rect))


MONITORS = [
    {"name": "eDP-1", "x": 0, "y": 0, "focused": False},
    {"name": "DP-2", "x": 1920, "y": 0, "focused": True},
]


class TestGrim:
    @pytest.fixture
    def monitors(self, monkeypatch):
        listed = list(MONITORS)
        monkeypatch.setattr(
            grim.hyprland, "query", lambda name: listed if name == "monitors" else []
        )
        return listed

    def test_the_command_names_one_output_and_writes_png_to_stdout(self):
        assert grim.grim_command("DP-2") == ["grim", "-o", "DP-2", "-"]
        assert grim.grim_command("") == ["grim", "-"]

    def test_grim_runs_without_a_shell_under_a_timeout(self, monkeypatch):
        seen = {}

        def run(command, **kwargs):
            seen.update(kwargs, command=command)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        monkeypatch.setattr(grim.subprocess, "run", run)

        grim._run(["grim", "-"])

        assert seen["command"] == ["grim", "-"]
        assert seen["timeout"] == grim.GRIM_TIMEOUT_S == 2.0
        assert seen["capture_output"] is True
        assert "shell" not in seen

    def test_output_is_the_screen_name_when_hyprland_knows_it(self, monitors):
        assert (
            grim.output_name(_fake_screen("eDP-1", QRect(0, 0, 1920, 1080))) == "eDP-1"
        )

    def test_output_falls_back_to_the_screen_position(self, monitors):
        screen = _fake_screen("XWAYLAND1", QRect(1920, 0, 2560, 1440))

        assert grim.output_name(screen) == "DP-2"

    def test_output_falls_back_to_the_focused_monitor(self, monitors):
        screen = _fake_screen("", QRect(5000, 5000, 100, 100))

        assert grim.output_name(screen) == "DP-2"

    def test_without_hyprctl_the_screen_name_is_used(self, monkeypatch):
        def broken(name):
            raise FileNotFoundError("hyprctl")

        monkeypatch.setattr(grim.hyprland, "query", broken)

        assert grim.output_name(_fake_screen("DP-2", QRect(0, 0, 10, 10))) == "DP-2"
        assert grim.focused_screen() is None

    def test_grab_runs_grim_for_the_screen_and_decodes_its_png(
        self, monkeypatch, monitors
    ):
        commands = []

        def run(command):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, _png(64, 40), b"")

        monkeypatch.setattr(grim, "_run", run)

        image = grim.grab(_fake_screen("DP-2", QRect(1920, 0, 32, 20)))

        assert commands == [["grim", "-o", "DP-2", "-"]]
        assert image.size() == QSize(64, 40)

    @pytest.mark.parametrize(
        ("code", "stdout"), [(1, b""), (0, b""), (0, b"not a png")]
    )
    def test_a_failed_grim_is_no_image(self, monkeypatch, monitors, code, stdout):
        monkeypatch.setattr(
            grim,
            "_run",
            lambda command: subprocess.CompletedProcess(command, code, stdout, b"boom"),
        )

        assert grim.grab(_fake_screen("DP-2", QRect(0, 0, 10, 10))) is None

    def test_the_focused_monitor_finds_its_qt_screen(self, monkeypatch):
        screen = QGuiApplication.primaryScreen()
        top_left = screen.geometry().topLeft()
        focused = {
            "name": "not-qt",
            "x": top_left.x(),
            "y": top_left.y(),
            "focused": True,
        }
        monkeypatch.setattr(grim.hyprland, "query", lambda name: [focused])

        assert grim.focused_screen() is screen

    def test_capture_goes_through_grim_on_hyprland(self, monkeypatch):
        monkeypatch.setattr(support, "_platform", lambda: "linux")
        monkeypatch.setattr(support.hyprland, "available", lambda: True)
        monkeypatch.setattr(
            grim, "grab", lambda screen: QImage(8, 6, QImage.Format.Format_RGB32)
        )

        image = REAL_GRAB(QGuiApplication.primaryScreen())

        assert image.size() == QSize(8, 6)


class TestCapture:
    def test_the_ratio_comes_from_the_captured_size(self, monkeypatch):
        screen = QGuiApplication.primaryScreen()
        monkeypatch.setattr(
            capture, "_grab_screen", lambda s: _screen_image(s, scale=2)
        )

        image = capture.grab(screen)

        assert image.devicePixelRatio() == 2.0
        assert image.deviceIndependentSize().toSize() == screen.geometry().size()

    def test_unpaintable_formats_are_converted(self, monkeypatch):
        screen = QGuiApplication.primaryScreen()
        indexed = _screen_image(screen).convertToFormat(QImage.Format.Format_Indexed8)
        monkeypatch.setattr(capture, "_grab_screen", lambda s: indexed)

        image = capture.grab(screen)

        assert image.format() == QImage.Format.Format_ARGB32_Premultiplied

    def test_hidden_windows_get_two_frames_or_hyprlands_fade(self, monkeypatch):
        screen = SimpleNamespace(refreshRate=lambda: 144.0)
        monkeypatch.setattr(support, "uses_grim", lambda: False)
        assert capture.settle_ms(screen) == capture.MIN_SETTLE_MS
        assert capture.settle_ms(SimpleNamespace(refreshRate=lambda: 30.0)) == 67

        monkeypatch.setattr(support, "uses_grim", lambda: True)
        assert capture.settle_ms(screen) == capture.HYPRLAND_SETTLE_MS
