"""Splash window can be dragged before the main window exists."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QApplication

from ui_qt.loading_screen import LoadingScreen


def test_loading_screen_drags_with_left_mouse():
    app = QApplication.instance() or QApplication([])
    screen = LoadingScreen()
    origin = screen.pos()

    press = SimpleNamespace(
        button=lambda: Qt.MouseButton.LeftButton,
        globalPosition=lambda: SimpleNamespace(
            toPoint=lambda: origin + QPoint(20, 20)
        ),
        accept=lambda: None,
    )
    screen.mousePressEvent(press)
    assert screen._drag_position is not None

    move = SimpleNamespace(
        buttons=lambda: Qt.MouseButton.LeftButton,
        globalPosition=lambda: SimpleNamespace(
            toPoint=lambda: origin + QPoint(60, 50)
        ),
        accept=lambda: None,
    )
    screen.mouseMoveEvent(move)
    assert screen.pos() == origin + QPoint(40, 30)

    release = SimpleNamespace(
        button=lambda: Qt.MouseButton.LeftButton,
        accept=lambda: None,
    )
    screen.mouseReleaseEvent(release)
    assert screen._drag_position is None

    screen.destroy()
    app.processEvents()
