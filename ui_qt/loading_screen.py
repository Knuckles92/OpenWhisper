"""
Modern PyQt6 Loading Screen.
Unified, custom-painted loading screen with a pulsing microphone glow.
"""
import logging
import math
import time
from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import Qt, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QPainterPath, QColor, QFont, QBrush, QPen,
    QLinearGradient, QRadialGradient
)

logger = logging.getLogger(__name__)

# Radians per second — full breath cycle about every 1.4s
_GLOW_RADIANS_PER_SEC = 4.5


class LoadingScreen(QWidget):
    """
    Unified modern loading screen with custom painting.
    Features a dark theme and a pulsing glow around the microphone icon.
    """

    # Signal to notify loading completion
    finished = pyqtSignal()

    def __init__(self):
        """Initialize loading screen."""
        super().__init__()

        # Window setup
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Size
        self.setFixedSize(450, 300)

        # Center on screen
        screen = QApplication.primaryScreen().geometry()
        self.move(
            screen.center().x() - self.width() // 2,
            screen.center().y() - self.height() // 2
        )

        self.status_text = "Initializing..."
        self.progress_text = "Please wait..."

        # Colors
        self.bg_color = QColor("#1c1c1e")  # Apple dark background
        self.accent_color = QColor("#0a84ff")  # Apple system blue
        self.text_color = QColor("#f5f5f7")  # Apple text white
        self.subtext_color = QColor("#8e8e93")  # Apple secondary label

        # Wall-clock phase so any paint (including startup processEvents)
        # shows the correct glow even if timer ticks were delayed.
        self._glow_started_at = time.monotonic()
        self._glow_timer = QTimer(self)
        self._glow_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._glow_timer.setInterval(33)  # ~30 FPS
        self._glow_timer.timeout.connect(self.update)
        self._glow_timer.start()

    def _glow_phase(self) -> float:
        """Return the current glow phase in radians from wall-clock time."""
        elapsed = time.monotonic() - self._glow_started_at
        return elapsed * _GLOW_RADIANS_PER_SEC

    def paintEvent(self, event):
        """Paint the custom loading screen."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        w, h = rect.width(), rect.height()

        # 1. Background with subtle gradient
        gradient = QLinearGradient(0, 0, 0, h)
        gradient.setColorAt(0, self.bg_color)
        gradient.setColorAt(1, self.bg_color.darker(120))

        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), 16, 16)

        painter.fillPath(path, gradient)

        # Border
        painter.setPen(QPen(QColor("#1e293b"), 1))  # Slate 800
        painter.drawPath(path)

        # 2. Central microphone with pulsing glow
        center_x, center_y = w / 2, h / 2 - 20
        phase = self._glow_phase()
        pulse = 0.5 + 0.5 * math.sin(phase)  # 0..1

        # Soft outer halo (expands slightly as it brightens)
        outer_radius = 58 + pulse * 14
        outer = QRadialGradient(center_x, center_y, outer_radius)
        outer.setColorAt(0, QColor(10, 132, 255, int(55 + pulse * 45)))
        outer.setColorAt(0.45, QColor(10, 132, 255, int(18 + pulse * 22)))
        outer.setColorAt(1, QColor(10, 132, 255, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(outer))
        painter.drawEllipse(QRectF(
            center_x - outer_radius, center_y - outer_radius,
            outer_radius * 2, outer_radius * 2,
        ))

        # Brighter inner core around the mic
        inner_radius = 28 + pulse * 6
        inner = QRadialGradient(center_x, center_y, inner_radius)
        inner.setColorAt(0, QColor(100, 210, 255, int(70 + pulse * 70)))
        inner.setColorAt(0.55, QColor(10, 132, 255, int(35 + pulse * 40)))
        inner.setColorAt(1, QColor(10, 132, 255, 0))
        painter.setBrush(QBrush(inner))
        painter.drawEllipse(QRectF(
            center_x - inner_radius, center_y - inner_radius,
            inner_radius * 2, inner_radius * 2,
        ))

        # Orbiting dots with a gentle brightness pulse
        num_dots = 5
        orbit_radius = 35
        for i in range(num_dots):
            angle = i * (2 * math.pi / num_dots) - math.pi / 2
            # Stagger each dot's pulse slightly for a breathing ring
            dot_pulse = 0.5 + 0.5 * math.sin(phase + i * 0.7)
            dot_x = center_x + math.cos(angle) * orbit_radius
            dot_y = center_y + math.sin(angle) * orbit_radius

            dot_size = 5 + dot_pulse * 2
            color = QColor(self.accent_color)
            color.setAlpha(int(140 + dot_pulse * 115))
            painter.setBrush(color)
            painter.drawEllipse(QRectF(
                dot_x - dot_size / 2, dot_y - dot_size / 2, dot_size, dot_size
            ))

        # Microphone icon — stroke brightens with the glow
        mic_w, mic_h = 16, 24
        mic_rect = QRectF(center_x - mic_w / 2, center_y - mic_h / 2, mic_w, mic_h)
        mic_alpha = int(200 + pulse * 55)
        mic_pen = QPen(QColor(255, 255, 255, mic_alpha), 2)
        painter.setPen(mic_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(mic_rect, 8, 8)
        painter.drawLine(int(center_x), int(center_y + 12), int(center_x), int(center_y + 18))
        painter.drawLine(int(center_x - 8), int(center_y + 18), int(center_x + 8), int(center_y + 18))

        # Soft fill inside the mic capsule on the bright half of the pulse
        if pulse > 0.35:
            fill = QColor(10, 132, 255, int((pulse - 0.35) / 0.65 * 55))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(mic_rect.adjusted(2, 2, -2, -2), 6, 6)

        # 3. Text
        # Title
        painter.setPen(self.text_color)
        painter.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        painter.drawText(QRectF(0, h - 90, w, 30), Qt.AlignmentFlag.AlignCenter, "OpenWhisper")

        # Status
        painter.setPen(self.accent_color)
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        painter.drawText(QRectF(0, h - 55, w, 20), Qt.AlignmentFlag.AlignCenter, self.status_text)

        # Progress/Details
        painter.setPen(self.subtext_color)
        painter.setFont(QFont("Segoe UI", 9))
        painter.drawText(QRectF(0, h - 35, w, 20), Qt.AlignmentFlag.AlignCenter, self.progress_text)

    def update_status(self, status_text: str):
        """Update the status message."""
        self.status_text = status_text
        self.update()

    def update_progress(self, progress_text: str):
        """Update the progress message."""
        self.progress_text = progress_text
        self.update()

    def closeEvent(self, event):
        """Handle closing."""
        self._glow_timer.stop()
        event.accept()
        logger.info("Loading screen closed")

    def destroy(self, destroyWindow=True, destroySubWindows=True):
        """Destroy the widget."""
        self._glow_timer.stop()
        super().destroy(destroyWindow, destroySubWindows)
