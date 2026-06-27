"""
Caret paste indicator overlay.
Shows a small animated marker while waiting to paste.

macOS has no public API to read the focused app's text caret position, so this
overlay tracks the mouse cursor as a graceful fallback (the Windows build used
win32 GetGUIThreadInfo to follow the real caret).
"""
import logging
import math
import time

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QTimer, QPointF
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QCursor


logger = logging.getLogger(__name__)


class CaretPasteIndicator(QWidget):
    """Animated overlay that tracks the caret position for pending paste."""

    def __init__(self):
        super().__init__()

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
        """Update widget position to track the mouse cursor.

        macOS exposes no public caret-position API, so the indicator follows the
        mouse cursor instead of the text caret.
        """
        cursor_pos = QCursor.pos()
        x = cursor_pos.x() - self._size // 2
        y = cursor_pos.y() - self._size // 2
        self.move(x, y)

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
