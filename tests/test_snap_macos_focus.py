import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget

from ui_qt.snap import affinity, live, macos
from ui_qt.snap.annotations import Tool


@pytest.fixture
def native(monkeypatch):
    owner = Mock()
    owner.isEqual_.return_value = False
    owner.isTerminated.return_value = False
    app = Mock()
    app.isActive.return_value = True
    workspace = SimpleNamespace(frontmostApplication=lambda: owner)
    appkit = SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace),
        NSRunningApplication=SimpleNamespace(currentApplication=lambda: object()),
        NSApplication=SimpleNamespace(sharedApplication=lambda: app),
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(macos, "_on_macos", lambda: True)
    monkeypatch.setattr(macos, "_focus_owner", None)
    return SimpleNamespace(owner=owner, app=app)


def test_drawing_focus_does_not_overwrite_a_snap_focus_owner(native, monkeypatch):
    snap_owner = object()
    monkeypatch.setattr(macos, "_focus_owner", snap_owner)
    previous = macos.foreground_app()
    assert previous is native.owner
    macos.return_focus_to(previous)
    native.app.yieldActivationToApplication_.assert_called_once_with(native.owner)
    native.owner.activateWithOptions_.assert_called_once_with(0)
    assert macos._focus_owner is snap_owner


@pytest.mark.parametrize("active,terminated", [(False, False), (True, True)])
def test_focus_return_respects_a_new_app_or_closed_owner(native, active, terminated):
    native.app.isActive.return_value = active
    native.owner.isTerminated.return_value = terminated
    macos.return_focus_to(native.owner)
    native.owner.activateWithOptions_.assert_not_called()


def test_snap_returns_its_focus_owner_once(native):
    macos.remember_focus()
    macos.return_focus()
    macos.return_focus()
    native.owner.activateWithOptions_.assert_called_once_with(0)


def test_affinity_only_returns_mac_focus_from_a_drawing_surface(monkeypatch):
    owner, canvas, other = object(), object(), object()
    monkeypatch.setattr(affinity, "QGuiApplication", SimpleNamespace(platformName=lambda: "cocoa"))
    monkeypatch.setattr(macos, "foreground_app", lambda: owner)
    restore = Mock()
    monkeypatch.setattr(macos, "return_focus_to", restore)
    assert affinity.foreground_window() is owner
    monkeypatch.setattr(affinity, "QApplication", SimpleNamespace(activeWindow=lambda: other))
    affinity.return_foreground(owner, [canvas])
    restore.assert_not_called()
    monkeypatch.setattr(affinity, "QApplication", SimpleNamespace(activeWindow=lambda: canvas))
    affinity.return_foreground(owner, [canvas])
    restore.assert_called_once_with(owner)


def test_mac_live_windows_stay_visible_and_controls_remain_above_canvases(monkeypatch):
    covers, levels, quiet, spaces = [], [], [], []
    monkeypatch.setattr(live.macos, "cover", covers.append)
    monkeypatch.setattr(live.macos, "make_nonactivating", quiet.append)
    monkeypatch.setattr(live.macos, "join_all_spaces", spaces.append)
    monkeypatch.setattr(live.macos, "set_level", lambda widget, level: levels.append((widget, level)) or 3)
    monkeypatch.setattr(live.affinity, "foreground_window", lambda: "previous app")
    monkeypatch.setattr(live.affinity, "bring_to_front", lambda _: None)
    restore = Mock()
    monkeypatch.setattr(live.affinity, "return_foreground", restore)
    panel = QWidget()
    drawing = live.LiveAnnotations(panel, panel)
    try:
        toolbar, = drawing.windows()
        assert toolbar in quiet and toolbar in covers
        assert toolbar in spaces and panel in spaces
        assert toolbar.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        drawing.arm(Tool.PEN)
        canvas = drawing._canvases[live.capture.screen_under_cursor()]
        assert canvas in covers
        assert canvas in spaces
        assert canvas.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        assert (toolbar, macos.PARKED_LEVEL) in levels
        assert (panel, macos.PARKED_LEVEL) in levels
        drawing.disarm()
        restore.assert_called_once_with("previous app", list(drawing._canvases.values()))
        assert levels[-1] == (panel, 3)
    finally:
        drawing.close()
        panel.close()


def test_live_windows_can_join_full_screen_spaces_without_conflicting_flags(monkeypatch):
    appkit = SimpleNamespace(
        NSWindowCollectionBehaviorMoveToActiveSpace=2,
        NSWindowCollectionBehaviorCanJoinAllSpaces=1,
        NSWindowCollectionBehaviorFullScreenAuxiliary=256,
    )
    window = Mock()
    window.collectionBehavior.return_value = 2 | 8
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(macos, "_on_macos", lambda: True)
    monkeypatch.setattr(macos, "_ns_window", lambda _: window)
    macos.join_all_spaces(object())
    window.setCollectionBehavior_.assert_called_once_with(1 | 8 | 256)
