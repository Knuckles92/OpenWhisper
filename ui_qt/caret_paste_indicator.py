"""
Caret paste indicator overlay.
Shows a small animated marker at the text caret while waiting to paste.
"""
import logging
import math
import sys
import time
import ctypes
from ctypes import wintypes
from typing import Optional, Tuple

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QTimer, QPointF, QPoint
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QCursor


if sys.platform == "win32":
    _USER32 = ctypes.windll.user32

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", _RECT),
        ]
else:
    _USER32 = None
    _RECT = None
    _GUITHREADINFO = None


class CaretPasteIndicator(QWidget):
    """Animated overlay that tracks the caret position for pending paste."""

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(__name__)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._size = 72
        self.setFixedSize(self._size, self._size)

        self._phase = 0.0
        self._last_frame_time = time.time()
        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)

        self.hide()

    def show_indicator(self):
        """Show the indicator and start animation."""
        self._phase = 0.0
        self._last_frame_time = time.time()
        self._update_position()
        self._timer.start(33)
        self.show()
        self.raise_()

    def hide_indicator(self):
        """Hide the indicator and stop animation."""
        self._timer.stop()
        self.hide()

    def _tick(self):
        """Advance animation and keep position aligned to caret."""
        now = time.time()
        dt = now - self._last_frame_time
        self._last_frame_time = now
        self._phase += dt * 2.6
        self._update_position()
        self.update()

    def _update_position(self):
        """Update widget position to track caret."""
        caret_center = self._get_caret_center()
        if caret_center is None:
            cursor_pos = QCursor.pos()
            caret_center = QPoint(cursor_pos.x(), cursor_pos.y())

        x = caret_center.x() - self._size // 2
        y = caret_center.y() - self._size // 2
        self.move(x, y)

    def _get_caret_center(self) -> Optional[QPoint]:
        """Get caret center in screen coordinates."""
        if _USER32 is None:
            return None

        try:
            foreground = _USER32.GetForegroundWindow()
            if not foreground:
                return None

            thread_id = _USER32.GetWindowThreadProcessId(foreground, None)
            gui = _GUITHREADINFO()
            gui.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if not _USER32.GetGUIThreadInfo(thread_id, ctypes.byref(gui)):
                return None

            if not gui.hwndCaret:
                return None

            top_left = wintypes.POINT(gui.rcCaret.left, gui.rcCaret.top)
            bottom_right = wintypes.POINT(gui.rcCaret.right, gui.rcCaret.bottom)

            if not _USER32.ClientToScreen(gui.hwndCaret, ctypes.byref(top_left)):
                return None
            if not _USER32.ClientToScreen(gui.hwndCaret, ctypes.byref(bottom_right)):
                return None

            x = int((top_left.x + bottom_right.x) / 2)
            y = int((top_left.y + bottom_right.y) / 2)

            x, y = self._scale_point_for_qt(gui.hwndCaret, x, y)
            return QPoint(x, y)
        except Exception as exc:
            self.logger.debug(f"Failed to read caret position: {exc}")
            return None

    def _scale_point_for_qt(self, hwnd, x: int, y: int) -> Tuple[int, int]:
        """Convert Win32 screen coords to Qt logical coords when DPI scaling is active."""
        if _USER32 is None or not hasattr(_USER32, "GetDpiForWindow"):
            return (x, y)

        try:
            dpi = _USER32.GetDpiForWindow(hwnd)
            if dpi:
                scale = dpi / 96.0
                return (int(x / scale), int(y / scale))
        except Exception:
            pass
        return (x, y)

    def paintEvent(self, event):
        """Draw the animated caret indicator."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        center = QPointF(self.width() / 2, self.height() / 2)
        pulse = (math.sin(self._phase) + 1.0) / 2.0

        base_radius = 10.0
        pulse_radius = base_radius + 6.0 * pulse
        orbit_radius = base_radius + 14.0

        glow_alpha = int(70 + 90 * pulse)
        ring_alpha = int(140 + 90 * pulse)

        glow_color = QColor(59, 130, 246, glow_alpha)
        ring_color = QColor(191, 219, 254, ring_alpha)
        dot_color = QColor(14, 165, 233, int(130 + 90 * pulse))
        caret_color = QColor(255, 255, 255, int(160 + 80 * pulse))

        # Soft glow ring
        painter.setPen(QPen(glow_color, 6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, pulse_radius + 4.0, pulse_radius + 4.0)

        # Crisp ring
        painter.setPen(QPen(ring_color, 2))
        painter.drawEllipse(center, pulse_radius, pulse_radius)

        # Orbiting dots
        for i in range(3):
            angle = self._phase * 1.6 + i * (2 * math.pi / 3)
            dx = math.cos(angle) * orbit_radius
            dy = math.sin(angle) * orbit_radius
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(dot_color))
            painter.drawEllipse(QPointF(center.x() + dx, center.y() + dy), 2.6, 2.6)

        # Caret highlight
        caret_height = 16.0
        painter.setPen(QPen(caret_color, 2))
        painter.drawLine(
            QPointF(center.x(), center.y() - caret_height / 2),
            QPointF(center.x(), center.y() + caret_height / 2)
        )

        painter.end()
