from abc import ABC, abstractmethod
from typing import List, Dict, Any
from PyQt6.QtGui import QPainter, QColor, QPen, QFont
from PyQt6.QtCore import QRect, Qt
import time
import math


def round_pen(color: QColor, width: float) -> QPen:
    """Pen with round caps/joins so drawn glyph strokes look polished."""
    return QPen(
        color, width, Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
    )


class BaseWaveformStyle(ABC):
    def __init__(self, width: int, height: int, config: Dict[str, Any]):
        self.width = width
        self.height = height
        self.config = config

        self.animation_time = 0.0
        self.last_frame_time = time.time()

        self.audio_levels: List[float] = []
        self.current_level = 0.0
        self.max_level = 0.0

        self._name = self.__class__.__name__.replace('Style', '').lower()
        self._display_name = self._name.title()
        self._description = "Custom waveform visualization style"

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def description(self) -> str:
        return self._description

    def update_audio_levels(self, levels: List[float], current_level: float = 0.0):
        self.audio_levels = levels.copy() if levels else []
        self.current_level = max(0.0, min(1.0, current_level))
        self.max_level = max(self.max_level * 0.99, self.current_level)

    def update_animation_time(self, delta_time: float):
        """Advance animation time by ``delta_time`` seconds."""
        self.animation_time += delta_time

    def get_cancellation_progress(self) -> float:
        """Return cancellation progress from 0.0 to 1.0."""
        from config import config

        if hasattr(self, '_canceling_start_time'):
            cancellation_duration = config.CANCELLATION_ANIMATION_DURATION_MS / 1000.0
            elapsed = time.time() - self._canceling_start_time
            return min(1.0, max(0.0, elapsed / cancellation_duration))
        return 0.0

    def set_canceling_start_time(self, start_time: float):
        """Set the cancellation start timestamp from ``time.time()``."""
        self._canceling_start_time = start_time

    @abstractmethod
    def draw_recording_state(self, painter: QPainter, rect: QRect, message: str = "Recording..."):
        pass

    @abstractmethod
    def draw_processing_state(self, painter: QPainter, rect: QRect, message: str = "Processing..."):
        pass

    @abstractmethod
    def draw_transcribing_state(self, painter: QPainter, rect: QRect, message: str = "Transcribing..."):
        pass

    def draw_canceling_state(self, painter: QPainter, rect: QRect, message: str = "Canceled"):
        """Draw the default shrinking cancel indicator."""
        progress = self.get_cancellation_progress()

        eased = 1.0 - (1.0 - progress) ** 3

        scale = 1.0 - eased
        opacity = int(255 * (1.0 - progress))

        center_x = rect.width() // 2
        center_y = rect.height() // 2
        size = int(40 * scale)

        color = QColor(255, 69, 58, opacity)
        painter.setPen(round_pen(color, 4))

        painter.drawLine(
            center_x - size, center_y - size,
            center_x + size, center_y + size
        )
        painter.drawLine(
            center_x + size, center_y - size,
            center_x - size, center_y + size
        )

        text_color = QColor(255, 255, 255, opacity)
        painter.setPen(text_color)
        font = QFont("Segoe UI", 10)
        painter.setFont(font)

        text_rect = QRect(0, rect.height() - 25, rect.width(), 20)
        painter.drawText(text_rect, 0x0004 | 0x0080, message)  # AlignCenter | AlignBottom

    def draw_stt_enable_state(self, painter: QPainter, rect: QRect, message: str = "STT Enabled"):
        center_x = rect.width() // 2
        center_y = rect.height() // 2

        color = QColor(48, 209, 88)
        painter.setPen(round_pen(color, 4))

        painter.drawLine(center_x - 15, center_y, center_x - 5, center_y + 10)
        painter.drawLine(center_x - 5, center_y + 10, center_x + 15, center_y - 10)

        painter.setPen(QColor(255, 255, 255))
        font = QFont("Segoe UI", 10)
        painter.setFont(font)

        text_rect = QRect(0, rect.height() - 25, rect.width(), 20)
        painter.drawText(text_rect, 0x0004 | 0x0080, message)

    def draw_stt_disable_state(self, painter: QPainter, rect: QRect, message: str = "STT Disabled"):
        center_x = rect.width() // 2
        center_y = rect.height() // 2
        size = 20

        color = QColor(255, 69, 58)
        painter.setPen(round_pen(color, 4))

        painter.drawLine(
            center_x - size, center_y - size,
            center_x + size, center_y + size
        )
        painter.drawLine(
            center_x + size, center_y - size,
            center_x - size, center_y + size
        )

        painter.setPen(QColor(255, 255, 255))
        font = QFont("Segoe UI", 10)
        painter.setFont(font)

        text_rect = QRect(0, rect.height() - 25, rect.width(), 20)
        painter.drawText(text_rect, 0x0004 | 0x0080, message)
