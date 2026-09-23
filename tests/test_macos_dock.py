import os
import sys
import types
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from ui_qt.utils.macos_dock import DockIconSync

REGULAR, ACCESSORY = 0, 1


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dock(qapp):
    ns_app = MagicMock()
    state = {"policy": REGULAR}
    ns_app.activationPolicy.side_effect = lambda: state["policy"]
    ns_app.setActivationPolicy_.side_effect = lambda p: state.update(policy=p)
    appkit = types.SimpleNamespace(
        NSApplication=types.SimpleNamespace(sharedApplication=lambda: ns_app),
        NSApplicationActivationPolicyRegular=REGULAR,
        NSApplicationActivationPolicyAccessory=ACCESSORY,
    )
    with patch.dict(sys.modules, {"AppKit": appkit}):
        sync = DockIconSync(qapp)
    widgets = []
    yield types.SimpleNamespace(ns_app=ns_app, state=state, widgets=widgets)
    for widget in widgets:
        widget.hide()
        widget.deleteLater()
    qapp.removeEventFilter(sync)
    sync.deleteLater()
    qapp.processEvents()


def _window(dock, cls=QWidget, flags=None):
    widget = cls()
    if flags is not None:
        widget.setWindowFlags(flags)
    dock.widgets.append(widget)
    return widget


def test_hiding_last_window_removes_dock_icon_and_showing_restores_it(qapp, dock):
    window = _window(dock)
    window.show()
    qapp.processEvents()
    assert dock.state["policy"] == REGULAR

    window.hide()
    qapp.processEvents()
    assert dock.state["policy"] == ACCESSORY

    window.show()
    assert dock.state["policy"] == REGULAR
    dock.ns_app.activateIgnoringOtherApps_.assert_called_with(True)


def test_open_dialog_keeps_dock_icon_while_main_window_is_hidden(qapp, dock):
    window = _window(dock)
    dialog = _window(dock, QDialog)
    window.show()
    dialog.show()
    window.hide()
    qapp.processEvents()
    assert dock.state["policy"] == REGULAR

    dialog.hide()
    qapp.processEvents()
    assert dock.state["policy"] == ACCESSORY


def test_floating_tool_windows_do_not_bring_dock_icon_back(qapp, dock):
    window = _window(dock)
    overlay = _window(
        dock, flags=Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
    )
    window.show()
    window.hide()
    qapp.processEvents()
    assert dock.state["policy"] == ACCESSORY

    overlay.show()
    qapp.processEvents()
    assert dock.state["policy"] == ACCESSORY
