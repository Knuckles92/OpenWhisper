from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QRect

from ui_qt.snap import capture


@pytest.mark.parametrize("point,index", [((600, 400), 0), ((-120, 25), 1), ((100, -500), 2)])
def test_mac_click_uses_desktop_points_across_retina_and_external_displays(monkeypatch, point, index):
    screens = [
        SimpleNamespace(area=QRect(0, 0, 1512, 982), dpr=2),
        SimpleNamespace(area=QRect(-1920, 0, 1920, 1080), dpr=1),
        SimpleNamespace(area=QRect(0, -1440, 2560, 1440), dpr=1.5),
    ]
    screen_at = Mock(side_effect=lambda at: next((s for s in screens if s.area.contains(at)), None))
    monkeypatch.setattr(capture.support, "_platform", lambda: "darwin")
    monkeypatch.setattr(capture, "QGuiApplication", SimpleNamespace(screenAt=screen_at))
    windows = Mock(side_effect=AssertionError("Mac clicks must not call Win32"))
    monkeypatch.setattr(capture, "screen_at_windows_point", windows)

    assert capture.screen_at_click(point) is screens[index]
    at, = screen_at.call_args.args
    assert (at.x(), at.y()) == point
    windows.assert_not_called()
    assert capture.screen_at_click((9000, 9000)) is None


def test_windows_click_keeps_native_monitor_mapping(monkeypatch):
    screen = object()
    native = Mock(return_value=screen)
    monkeypatch.setattr(capture.support, "_platform", lambda: "win32")
    monkeypatch.setattr(capture, "screen_at_windows_point", native)
    assert capture.screen_at_click((-1800, 400)) is screen
    native.assert_called_once_with((-1800, 400))
