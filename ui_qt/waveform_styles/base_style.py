"""
Base class for all PyQt6 waveform overlay styles.
Defines the interface that all styles must implement.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from PyQt6.QtGui import QPainter, QColor, QPen, QFont
from PyQt6.QtCore import QRect
import time
import math


class BaseWaveformStyle(ABC):
    """Abstract base class for waveform overlay styles."""

    def __init__(self, width: int, height: int, config: Dict[str, Any]):
        """Initialize the style.

        Args:
            width: Canvas width in pixels
            height: Canvas height in pixels
            config: Style-specific configuration dictionary
        """
        self.width = width
        self.height = height
        self.config = config

        # Animation state
        self.animation_time = 0.0
        self.last_frame_time = time.time()

        # Audio data
        self.audio_levels: List[float] = []
        self.current_level = 0.0
        self.max_level = 0.0

        # Style metadata
        self._name = self.__class__.__name__.replace('Style', '').lower()
        self._display_name = self._name.title()
        self._description = "Custom waveform visualization style"

    @property
    def name(self) -> str:
        """Get the internal name of this style."""
        return self._name

    @property
    def display_name(self) -> str:
        """Get the display name of this style."""
        return self._display_name

    @property
    def description(self) -> str:
        """Get the description of this style."""
        return self._description

    def update_audio_levels(self, levels: List[float], current_level: float = 0.0):
        """Update audio levels for visualization.

        Args:
            levels: List of audio levels for each frequency band/bar
            current_level: Current overall audio level (0.0 to 1.0)
        """
        self.audio_levels = levels.copy() if levels else []
        self.current_level = max(0.0, min(1.0, current_level))
        self.max_level = max(self.max_level * 0.99, self.current_level)

    def update_animation_time(self, delta_time: float):
        """Update the animation time.

        Args:
            delta_time: Time elapsed since last frame in seconds
        """
        self.animation_time += delta_time

    def get_cancellation_progress(self) -> float:
        """Get cancellation animation progress (0.0 to 1.0).

        Returns:
            Progress from 0.0 (start) to 1.0 (end)
        """
        from config import config

        if hasattr(self, '_canceling_start_time'):
            cancellation_duration = config.CANCELLATION_ANIMATION_DURATION_MS / 1000.0
            elapsed = time.time() - self._canceling_start_time
            return min(1.0, max(0.0, elapsed / cancellation_duration))
        return 0.0

    def set_canceling_start_time(self, start_time: float):
        """Set the cancellation animation start time.

        Args:
            start_time: Start time from time.time()
        """
        self._canceling_start_time = start_time

    @abstractmethod
    def draw_recording_state(self, painter: QPainter, rect: QRect, message: str = "Recording..."):
        """Draw the recording state visualization.

        Args:
            painter: QPainter instance for drawing
            rect: Drawing area rectangle
            message: Status message to display
        """
        pass

    @abstractmethod
    def draw_processing_state(self, painter: QPainter, rect: QRect, message: str = "Processing..."):
        """Draw the processing state visualization.

        Args:
            painter: QPainter instance for drawing
            rect: Drawing area rectangle
            message: Status message to display
        """
        pass

    @abstractmethod
    def draw_transcribing_state(self, painter: QPainter, rect: QRect, message: str = "Transcribing..."):
        """Draw the transcribing state visualization.

        Args:
            painter: QPainter instance for drawing
            rect: Drawing area rectangle
            message: Status message to display
        """
        pass

    def draw_canceling_state(self, painter: QPainter, rect: QRect, message: str = "Cancelled"):
        """Draw a universal canceling animation with shrinking red X.

        Args:
            painter: QPainter instance for drawing
            rect: Drawing area rectangle
            message: Status message to display
        """
        progress = self.get_cancellation_progress()

        # Easing for smooth animation
        eased = 1.0 - (1.0 - progress) ** 3

        # Calculate scale (shrinks from 1.0 to 0.0)
        scale = 1.0 - eased
        opacity = int(255 * (1.0 - progress))

        # Draw red X
        center_x = rect.width() // 2
        center_y = rect.height() // 2
        size = int(40 * scale)

        # Draw X lines
        color = QColor(255, 68, 68, opacity)
        pen = QPen(color, 4)
        painter.setPen(pen)

        painter.drawLine(
            center_x - size, center_y - size,
            center_x + size, center_y + size
        )
        painter.drawLine(
            center_x + size, center_y - size,
            center_x - size, center_y + size
        )

        # Draw text with fade
        text_color = QColor(255, 255, 255, opacity)
        painter.setPen(text_color)
        font = QFont("Segoe UI", 10)
        painter.setFont(font)

        text_rect = QRect(0, rect.height() - 25, rect.width(), 20)
        painter.drawText(text_rect, 0x0004 | 0x0080, message)  # AlignCenter | AlignBottom

    def draw_stt_enable_state(self, painter: QPainter, rect: QRect, message: str = "STT Enabled"):
        """Draw STT enable state with green checkmark animation.

        Args:
            painter: QPainter instance for drawing
            rect: Drawing area rectangle
            message: Status message to display
        """
        # Simple checkmark for now
        center_x = rect.width() // 2
        center_y = rect.height() // 2

        # Draw green checkmark
        color = QColor(16, 185, 129)  # Emerald green
        pen = QPen(color, 4)
        painter.setPen(pen)

        # Checkmark path
        painter.drawLine(center_x - 15, center_y, center_x - 5, center_y + 10)
        painter.drawLine(center_x - 5, center_y + 10, center_x + 15, center_y - 10)

        # Draw text
        painter.setPen(QColor(255, 255, 255))
        font = QFont("Segoe UI", 10)
        painter.setFont(font)

        text_rect = QRect(0, rect.height() - 25, rect.width(), 20)
        painter.drawText(text_rect, 0x0004 | 0x0080, message)

    def draw_stt_disable_state(self, painter: QPainter, rect: QRect, message: str = "STT Disabled"):
        """Draw STT disable state with red X animation.

        Args:
            painter: QPainter instance for drawing
            rect: Drawing area rectangle
            message: Status message to display
        """
        center_x = rect.width() // 2
        center_y = rect.height() // 2
        size = 20

        # Draw red X
        color = QColor(239, 68, 68)  # Red
        pen = QPen(color, 4)
        painter.setPen(pen)

        painter.drawLine(
            center_x - size, center_y - size,
            center_x + size, center_y + size
        )
        painter.drawLine(
            center_x + size, center_y - size,
            center_x - size, center_y + size
        )

        # Draw text
        painter.setPen(QColor(255, 255, 255))
        font = QFont("Segoe UI", 10)
        painter.setFont(font)

        text_rect = QRect(0, rect.height() - 25, rect.width(), 20)
        painter.drawText(text_rect, 0x0004 | 0x0080, message)
