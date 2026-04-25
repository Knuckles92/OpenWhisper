"""
Modern PyQt6 Loading Screen.
Unified, custom-painted loading screen with static display for fast startup.
"""
import logging
import math
from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import Qt, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QPainterPath, QColor, QFont, QBrush, QPen,
    QLinearGradient, QRadialGradient
)


class ModernLoadingScreen(QWidget):
    """
    Unified modern loading screen with custom painting.
    Features a dark theme with static display for fast startup.
    """

    # Signal to notify loading completion
    finished = pyqtSignal()

    def __init__(self):
        """Initialize loading screen."""
        super().__init__()
        self.logger = logging.getLogger(__name__)

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
        self.bg_color = QColor("#0f172a")  # Slate 900
        self.accent_color = QColor("#6366f1")  # Indigo 500
        self.text_color = QColor("#e2e8f0")  # Slate 200
        self.subtext_color = QColor("#94a3b8")  # Slate 400

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

        # 2. Central static display
        center_x, center_y = w / 2, h / 2 - 20

        # Draw static glow
        radius = 50
        radial = QRadialGradient(center_x, center_y, radius)
        radial.setColorAt(0, QColor(99, 102, 241, 40))  # Indigo
        radial.setColorAt(1, QColor(99, 102, 241, 0))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(radial))
        painter.drawEllipse(QRectF(center_x - radius, center_y - radius, radius * 2, radius * 2))

        # Draw static dots in circle
        num_dots = 5
        orbit_radius = 35
        for i in range(num_dots):
            angle = i * (2 * math.pi / num_dots) - math.pi / 2  # Start from top
            dot_x = center_x + math.cos(angle) * orbit_radius
            dot_y = center_y + math.sin(angle) * orbit_radius

            dot_size = 6
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.accent_color)
            painter.drawEllipse(QRectF(dot_x - dot_size/2, dot_y - dot_size/2, dot_size, dot_size))

        # Draw central icon/logo placeholder (Microphone shape simplified)
        mic_w, mic_h = 16, 24
        mic_rect = QRectF(center_x - mic_w/2, center_y - mic_h/2, mic_w, mic_h)
        painter.setPen(QPen(QColor("#ffffff"), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(mic_rect, 8, 8)
        painter.drawLine(int(center_x), int(center_y + 12), int(center_x), int(center_y + 18))
        painter.drawLine(int(center_x - 8), int(center_y + 18), int(center_x + 8), int(center_y + 18))

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
        event.accept()
        self.logger.info("Loading screen closed")

    def destroy(self, destroyWindow=True, destroySubWindows=True):
        """Destroy the widget."""
        super().destroy(destroyWindow, destroySubWindows)
