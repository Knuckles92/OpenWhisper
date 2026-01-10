"""
Streaming Text Overlay for PyQt6 Application.
Displays streaming transcription text in real-time during recording.
Replaces the fragile keyboard simulation approach with a clean popup.
"""
import logging
import time
from typing import Optional, List
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QScrollArea, QGraphicsOpacityEffect
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QBrush, QPen, QFont, QCursor, QPainterPath

from config import config


class StreamingTextOverlay(QWidget):
    """Overlay for displaying streaming transcription text in real-time."""

    # Signals
    state_changed = pyqtSignal(str)
    hidden = pyqtSignal()

    # States
    STATE_IDLE = "idle"
    STATE_STREAMING = "streaming"
    STATE_FINALIZING = "finalizing"

    def __init__(self):
        """Initialize the streaming text overlay."""
        super().__init__()
        self.logger = logging.getLogger(__name__)

        # Window properties - frameless, always on top
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Size configuration
        self.overlay_width = getattr(config, 'STREAMING_OVERLAY_WIDTH', 450)
        self.min_height = getattr(config, 'STREAMING_OVERLAY_MIN_HEIGHT', 100)
        self.max_height = getattr(config, 'STREAMING_OVERLAY_MAX_HEIGHT', 300)
        self.font_size = getattr(config, 'STREAMING_OVERLAY_FONT_SIZE', 12)

        self.setMinimumWidth(self.overlay_width)
        self.setMaximumWidth(self.overlay_width)
        self.setMinimumHeight(self.min_height)
        self.setMaximumHeight(self.max_height)

        # State
        self.current_state = self.STATE_IDLE
        self._text_chunks: List[str] = []  # Accumulated finalized chunks
        self._current_partial: str = ""  # Current non-finalized text

        # Animation
        self._animation_time = 0.0
        self._last_frame_time = time.time()
        self._pulse_phase = 0.0

        self._animation_timer = QTimer()
        self._animation_timer.timeout.connect(self._update_animation)

        # Fade animation
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(1.0)

        self._fade_animation = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_animation.setDuration(200)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

        # Setup UI
        self._setup_ui()

        # Hide by default
        self.hide()

    def _setup_ui(self):
        """Setup the overlay UI components."""
        # Main layout with margins for rounded border
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Header label
        self._header_label = QLabel("Streaming...")
        self._header_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._header_label.setStyleSheet("color: #a5b4fc; background: transparent;")
        layout.addWidget(self._header_label)

        # Scroll area for text
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll_area.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: rgba(45, 45, 68, 100);
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: rgba(99, 102, 241, 150);
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

        # Text label inside scroll area
        self._text_label = QLabel("")
        self._text_label.setFont(QFont("Segoe UI", self.font_size))
        self._text_label.setStyleSheet("color: #e0e0ff; background: transparent;")
        self._text_label.setWordWrap(True)
        self._text_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._text_label.setTextFormat(Qt.TextFormat.PlainText)

        self._scroll_area.setWidget(self._text_label)
        layout.addWidget(self._scroll_area, 1)  # Stretch to fill

    def paintEvent(self, event):
        """Custom paint for rounded semi-transparent background."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        rect = self.rect()
        path = QPainterPath()
        path.addRoundedRect(float(rect.x()), float(rect.y()),
                           float(rect.width()), float(rect.height()), 12, 12)

        # Semi-transparent dark background
        painter.fillPath(path, QBrush(QColor(45, 45, 68, 230)))

        # Border with pulsing effect when streaming
        if self.current_state == self.STATE_STREAMING:
            # Pulsing border color
            pulse = (1 + abs(self._pulse_phase)) / 2  # 0.5 to 1.0
            alpha = int(100 + 100 * pulse)
            border_color = QColor(99, 102, 241, alpha)
        else:
            border_color = QColor(99, 102, 241, 150)

        painter.setPen(QPen(border_color, 2))
        painter.drawPath(path)

        # Recording indicator dot (pulsing)
        if self.current_state == self.STATE_STREAMING:
            pulse = (1 + abs(self._pulse_phase)) / 2
            dot_alpha = int(150 + 105 * pulse)
            dot_size = 6 + 2 * pulse

            painter.setBrush(QBrush(QColor(239, 68, 68, dot_alpha)))  # Red
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(int(20 - dot_size / 2), int(20 - dot_size / 2),
                               int(dot_size), int(dot_size))

        painter.end()

    def _update_animation(self):
        """Update animation state."""
        current_time = time.time()
        delta_time = current_time - self._last_frame_time
        self._last_frame_time = current_time

        self._animation_time += delta_time

        # Update pulse phase (sine wave)
        self._pulse_phase = abs(self._animation_time * 4) % 2 - 1  # -1 to 1

        # Update display text with animated ellipsis
        self._update_display_text()

        self.update()

    def _update_display_text(self):
        """Update the display with current text and animated ellipsis."""
        # Combine all finalized chunks
        full_text = " ".join(self._text_chunks)

        # Add current partial if exists
        if self._current_partial:
            if full_text:
                full_text += " " + self._current_partial
            else:
                full_text = self._current_partial

        # Add animated ellipsis when streaming and we have text
        if self.current_state == self.STATE_STREAMING and full_text:
            dots = "." * (1 + int(self._animation_time * 2) % 3)
            full_text += dots

        self._text_label.setText(full_text)

        # Auto-scroll to bottom
        scrollbar = self._scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

        # Adjust height based on content
        self._adjust_height()

    def _adjust_height(self):
        """Adjust overlay height based on text content."""
        # Get the ideal height for the text
        text_height = self._text_label.sizeHint().height()
        header_height = self._header_label.sizeHint().height()

        # Calculate total needed height (text + header + margins + spacing)
        margins = 24  # Top + bottom margins
        spacing = 8
        needed_height = text_height + header_height + margins + spacing + 20  # Extra padding

        # Clamp to min/max
        new_height = max(self.min_height, min(self.max_height, needed_height))

        if self.height() != new_height:
            self.setFixedHeight(int(new_height))

    def update_streaming_text(self, text: str, is_final: bool):
        """Update the streaming transcription text.

        Args:
            text: The transcription text chunk
            is_final: Whether this chunk is finalized
        """
        if is_final:
            # Add to finalized chunks
            if text.strip():
                self._text_chunks.append(text.strip())
            self._current_partial = ""
        else:
            # Update partial text
            self._current_partial = text.strip() if text else ""

        self._update_display_text()
        self.logger.debug(f"Streaming text updated: chunks={len(self._text_chunks)}, partial={bool(self._current_partial)}")

    def show_at_cursor(self, state: Optional[str] = None):
        """Show overlay near the cursor.

        Args:
            state: Optional state to set. Defaults to STREAMING.
        """
        # Get global cursor position
        cursor_pos = QCursor.pos()

        # Position overlay near cursor (offset slightly)
        x = cursor_pos.x() + 15
        y = cursor_pos.y() + 15

        self.move(x, y)

        # Set state
        if state is not None:
            self.set_state(state)
        elif self.current_state == self.STATE_IDLE:
            self.set_state(self.STATE_STREAMING)

        # Fade in
        self._fade_animation.stop()
        self._opacity_effect.setOpacity(0.0)
        self._fade_animation.setStartValue(0.0)
        self._fade_animation.setEndValue(1.0)
        self._fade_animation.start()

        self.show()
        self.raise_()

    def hide_with_animation(self):
        """Hide the overlay with fade-out animation."""
        self._fade_animation.stop()
        self._fade_animation.setStartValue(self._opacity_effect.opacity())
        self._fade_animation.setEndValue(0.0)
        self._fade_animation.finished.connect(self._on_fade_out_finished)
        self._fade_animation.start()

    def _on_fade_out_finished(self):
        """Called when fade-out animation completes."""
        self._fade_animation.finished.disconnect(self._on_fade_out_finished)
        self.hide()
        self.set_state(self.STATE_IDLE)
        self.hidden.emit()

    def set_state(self, state: str):
        """Set the overlay state.

        Args:
            state: The state to set (STATE_IDLE, STATE_STREAMING, STATE_FINALIZING)
        """
        if self.current_state != state:
            self.current_state = state
            self._animation_time = 0.0
            self._pulse_phase = 0.0
            self._last_frame_time = time.time()

            # Update header text based on state
            if state == self.STATE_STREAMING:
                self._header_label.setText("Streaming...")
                self._animation_timer.start(33)  # ~30 FPS
            elif state == self.STATE_FINALIZING:
                self._header_label.setText("Finalizing...")
                self._animation_timer.start(33)
            else:
                self._header_label.setText("")
                self._animation_timer.stop()

            self.state_changed.emit(state)
            self.logger.debug(f"Streaming overlay state changed to: {state}")

    def clear_text(self):
        """Clear all accumulated text."""
        self._text_chunks = []
        self._current_partial = ""
        self._text_label.setText("")
        self.setFixedHeight(self.min_height)
        self.logger.debug("Streaming text cleared")

    def get_accumulated_text(self) -> str:
        """Get all accumulated finalized text.

        Returns:
            Combined text from all finalized chunks.
        """
        return " ".join(self._text_chunks)

    def cleanup(self):
        """Clean up resources."""
        self._animation_timer.stop()
        self._fade_animation.stop()
        self.close()
