"""Carrying a frameless floating window with the pointer."""

from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QWidget

from ui_qt.utils.overlay_position import clamp_rect_to_available


class DragToMove:
    """Moves ``window`` while the left button holds it, kept on the pointer's screen.

    The window forwards its mouse events here; each handler returns True when
    the event was the drag's. ``moved`` stays set until ``reset``, so a
    window that places itself can leave a spot the user chose alone.
    """

    def __init__(self, window: QWidget) -> None:
        self._window = window
        self._grab: QPoint | None = None
        self.moved = False
        window.setCursor(Qt.CursorShape.OpenHandCursor)

    def press(self, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        self._grab = event.globalPosition().toPoint() - self._window.pos()
        self._window.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()
        return True

    def move(self, event: QMouseEvent) -> bool:
        if self._grab is None:
            return False
        pointer = event.globalPosition().toPoint()
        target = pointer - self._grab
        if target != self._window.pos():
            x, y = target.x(), target.y()
            screen = QApplication.screenAt(pointer) or self._window.screen()
            if screen is not None:
                size = self._window.size()
                x, y = clamp_rect_to_available(x, y, size.width(), size.height(),
                                               screen.availableGeometry())
            self._window.move(x, y)
            self.moved = True
        event.accept()
        return True

    def release(self, event: QMouseEvent) -> bool:
        if self._grab is None or event.button() != Qt.MouseButton.LeftButton:
            return False
        self._grab = None
        self._window.setCursor(Qt.CursorShape.OpenHandCursor)
        event.accept()
        return True

    def reset(self) -> None:
        """Forget the drag: the window's next placement is its own again."""
        self._grab = None
        self.moved = False
