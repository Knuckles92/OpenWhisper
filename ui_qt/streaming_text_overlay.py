"""
Streaming Text Overlay for PyQt6 Application.
Displays streaming transcription text in real-time during recording.
Replaces the fragile keyboard simulation approach with a clean popup.
"""
import logging
import time
from typing import Optional, List, Tuple
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QScrollArea, QGraphicsOpacityEffect, QApplication
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtSignal, QPoint
from PyQt6.QtGui import QPainter, QColor, QBrush, QPen, QFont, QCursor, QPainterPath

from config import config
from services.settings import settings_manager


class StreamingTextOverlay(QWidget):
    """Overlay for displaying streaming transcription text in real-time."""

    # Signals
    state_changed = pyqtSignal(str)
    hidden = pyqtSignal()

    # States
    STATE_IDLE = "idle"
    STATE_STREAMING = "streaming"
    STATE_FINALIZING = "finalizing"
    STATE_CANCELING = "canceling"

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

        # Drag state
        self._drag_position: Optional[QPoint] = None
        self._is_dragging = False

        # Set cursor to indicate draggable
        self.setCursor(Qt.CursorShape.OpenHandCursor)

        # Animation
        self._animation_time = 0.0
        self._last_frame_time = time.time()
        self._pulse_phase = 0.0

        self._animation_timer = QTimer()
        self._animation_timer.timeout.connect(self._update_animation)

        # Cancel animation properties
        self._cancel_progress = 0.0
        self._cancel_duration = config.CANCELLATION_ANIMATION_DURATION_MS / 1000.0  # seconds
        self._cancel_particles: List[dict] = []  # Particles for cancel effect
        self._cancel_flash_intensity = 0.0  # Red flash intensity (0.0 to 1.0)

        # Fade animation
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(1.0)

        self._fade_animation = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_animation.setDuration(200)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        # Connect finished signal once, use flag to track fade direction
        self._is_fading_out = False
        self._fade_animation.finished.connect(self._on_fade_animation_finished)

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

        # Semi-transparent dark background (with red tint when canceling)
        if self.current_state == self.STATE_CANCELING:
            # Blend in red based on flash intensity
            red_blend = int(45 + 30 * self._cancel_flash_intensity)
            green_blend = int(45 - 20 * self._cancel_flash_intensity)
            blue_blend = int(68 - 30 * self._cancel_flash_intensity)
            bg_color = QColor(red_blend, green_blend, blue_blend, 230)
        else:
            bg_color = QColor(45, 45, 68, 230)

        painter.fillPath(path, QBrush(bg_color))

        # Border color based on state
        if self.current_state == self.STATE_STREAMING:
            # Pulsing border color
            pulse = (1 + abs(self._pulse_phase)) / 2  # 0.5 to 1.0
            alpha = int(100 + 100 * pulse)
            border_color = QColor(99, 102, 241, alpha)
        elif self.current_state == self.STATE_CANCELING:
            # Red border with flash effect
            flash = self._cancel_flash_intensity
            # Transition from purple to red
            r = int(99 + (239 - 99) * flash)
            g = int(102 - 34 * flash)  # 102 -> 68
            b = int(241 - 173 * flash)  # 241 -> 68
            alpha = int(180 + 75 * (1 - self._cancel_progress))
            border_color = QColor(r, g, b, alpha)
        else:
            border_color = QColor(99, 102, 241, 150)

        # Draw border (thicker when canceling)
        border_width = 3 if self.current_state == self.STATE_CANCELING else 2
        painter.setPen(QPen(border_color, border_width))
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

        # Cancel animation effects
        if self.current_state == self.STATE_CANCELING:
            self._draw_cancel_effects(painter, rect)

        painter.end()

    def _init_cancel_particles(self):
        """Initialize particles for cancel animation."""
        import random
        self._cancel_particles = []
        rect = self.rect()
        center_x = rect.width() / 2
        center_y = rect.height() / 2

        # Create particles that will scatter outward
        for _ in range(20):
            angle = random.uniform(0, 2 * 3.14159)
            speed = random.uniform(80, 200)
            self._cancel_particles.append({
                'x': center_x,
                'y': center_y,
                'vx': speed * (angle - 3.14159) / 3.14159,  # Velocity X
                'vy': speed * ((angle - 3.14159 / 2) / 3.14159),  # Velocity Y
                'size': random.uniform(3, 8),
                'alpha': 255,
                'decay': random.uniform(0.85, 0.95),
            })

    def _update_cancel_particles(self, delta_time: float):
        """Update cancel particle positions and properties."""
        for p in self._cancel_particles:
            p['x'] += p['vx'] * delta_time
            p['y'] += p['vy'] * delta_time
            p['alpha'] = int(p['alpha'] * p['decay'])
            p['size'] *= 0.98

    def _draw_cancel_effects(self, painter: QPainter, rect):
        """Draw cancel animation effects including particles and X mark."""
        progress = self._cancel_progress
        fade_alpha = max(0, int(255 * (1 - progress * 0.8)))

        # Draw scatter particles
        painter.setPen(Qt.PenStyle.NoPen)
        for p in self._cancel_particles:
            if p['alpha'] > 10:
                color = QColor(239, 68, 68, int(p['alpha'] * (1 - progress)))
                painter.setBrush(QBrush(color))
                painter.drawEllipse(int(p['x'] - p['size'] / 2),
                                   int(p['y'] - p['size'] / 2),
                                   int(p['size']), int(p['size']))

        # Draw animated X mark in center
        center_x = rect.width() // 2
        center_y = rect.height() // 2

        # X size shrinks and fades as animation progresses
        x_size = int(20 * (1 - progress * 0.5))
        x_alpha = fade_alpha

        if x_alpha > 0:
            pen = QPen(QColor(239, 68, 68, x_alpha), 3)
            painter.setPen(pen)
            painter.drawLine(center_x - x_size, center_y - x_size,
                           center_x + x_size, center_y + x_size)
            painter.drawLine(center_x + x_size, center_y - x_size,
                           center_x - x_size, center_y + x_size)

    def _update_animation(self):
        """Update animation state."""
        current_time = time.time()
        delta_time = current_time - self._last_frame_time
        self._last_frame_time = current_time

        self._animation_time += delta_time

        # Handle cancel animation
        if self.current_state == self.STATE_CANCELING:
            self._cancel_progress = min(1.0, self._animation_time / self._cancel_duration)

            # Flash intensity decays quickly at start
            self._cancel_flash_intensity = max(0, 1.0 - self._animation_time * 3)

            # Update particles
            self._update_cancel_particles(delta_time)

            # Check if animation is complete
            if self._cancel_progress >= 1.0:
                self.logger.debug("Cancel animation complete, hiding overlay")
                self._animation_timer.stop()
                self.hide_with_animation()
                return

        # Update pulse phase (sine wave)
        self._pulse_phase = abs(self._animation_time * 4) % 2 - 1  # -1 to 1

        # Update display text with animated ellipsis (not during cancel)
        if self.current_state != self.STATE_CANCELING:
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
        self._is_fading_out = False
        self._opacity_effect.setOpacity(0.0)
        self._fade_animation.setStartValue(0.0)
        self._fade_animation.setEndValue(1.0)
        self._fade_animation.start()

        self.show()
        self.raise_()

    def hide_with_animation(self):
        """Hide the overlay with fade-out animation."""
        if self._is_fading_out:
            return  # Already fading out
        self._fade_animation.stop()
        self._is_fading_out = True
        self._fade_animation.setStartValue(self._opacity_effect.opacity())
        self._fade_animation.setEndValue(0.0)
        self._fade_animation.start()

    def show_cancel_animation(self):
        """Show cancel animation and auto-hide when complete.

        Transitions the overlay to cancel state with visual effects,
        then automatically hides after the animation completes.
        """
        if self.current_state == self.STATE_CANCELING:
            return  # Already canceling

        self.logger.debug("Starting streaming overlay cancel animation")

        # Initialize cancel state
        self._cancel_progress = 0.0
        self._cancel_flash_intensity = 1.0  # Start with full flash
        self._init_cancel_particles()

        # Set state (this will update header and start animation timer)
        self.set_state(self.STATE_CANCELING)

    def _on_fade_animation_finished(self):
        """Called when any fade animation completes."""
        if self._is_fading_out:
            self._is_fading_out = False
            self.hide()
            self.set_state(self.STATE_IDLE)
            self.hidden.emit()

    def set_state(self, state: str):
        """Set the overlay state.

        Args:
            state: The state to set (STATE_IDLE, STATE_STREAMING, STATE_FINALIZING, STATE_CANCELING)
        """
        if self.current_state != state:
            self.current_state = state
            self._animation_time = 0.0
            self._pulse_phase = 0.0
            self._last_frame_time = time.time()

            # Update header text based on state
            if state == self.STATE_STREAMING:
                self._header_label.setText("Streaming...")
                self._header_label.setStyleSheet("color: #a5b4fc; background: transparent;")
                self._animation_timer.start(33)  # ~30 FPS
            elif state == self.STATE_FINALIZING:
                self._header_label.setText("Finalizing...")
                self._header_label.setStyleSheet("color: #a5b4fc; background: transparent;")
                self._animation_timer.start(33)
            elif state == self.STATE_CANCELING:
                self._header_label.setText("Cancelled")
                self._header_label.setStyleSheet("color: #ef4444; background: transparent;")
                self._animation_timer.start(33)
            else:
                self._header_label.setText("")
                self._header_label.setStyleSheet("color: #a5b4fc; background: transparent;")
                self._animation_timer.stop()

            self.state_changed.emit(state)
            self.logger.debug(f"Streaming overlay state changed to: {state}")

    def clear_text(self):
        """Clear all accumulated text and reset state for new session."""
        self._text_chunks = []
        self._current_partial = ""
        self._text_label.setText("")
        self.setFixedHeight(self.min_height)
        self._is_fading_out = False  # Reset fade state for new session
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

    # -------------------------------------------------------------------------
    # Drag Functionality
    # -------------------------------------------------------------------------

    def mousePressEvent(self, event):
        """Handle mouse press for dragging."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_dragging = True
            self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Handle mouse move for dragging."""
        if self._is_dragging and event.buttons() == Qt.MouseButton.LeftButton and self._drag_position:
            self.move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """Handle mouse release to end drag and save position."""
        if event.button() == Qt.MouseButton.LeftButton and self._is_dragging:
            self._is_dragging = False
            self._drag_position = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self._save_position()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    # -------------------------------------------------------------------------
    # Position Management
    # -------------------------------------------------------------------------

    def _save_position(self):
        """Save current position to settings."""
        try:
            pos = self.pos()
            settings_manager.save_streaming_overlay_position(pos.x(), pos.y())
            self.logger.debug(f"Streaming overlay position saved: {pos.x()}, {pos.y()}")
        except Exception as e:
            self.logger.warning(f"Failed to save streaming overlay position: {e}")

    def _get_screen_center(self) -> QPoint:
        """Get the center position of the primary screen.

        Returns:
            QPoint at screen center, adjusted for overlay size.
        """
        screen = QApplication.primaryScreen()
        if screen:
            screen_geo = screen.availableGeometry()
            center_x = screen_geo.center().x() - self.width() // 2
            center_y = screen_geo.center().y() - self.height() // 2
            return QPoint(center_x, center_y)
        # Fallback if no screen detected
        return QPoint(100, 100)

    def _validate_position(self, x: int, y: int) -> Tuple[int, int]:
        """Validate position is within screen bounds.

        Args:
            x: X coordinate
            y: Y coordinate

        Returns:
            Tuple of validated (x, y) coordinates.
        """
        screen = QApplication.primaryScreen()
        if screen:
            screen_geo = screen.availableGeometry()
            # Ensure at least part of the overlay is visible
            # Clamp x to keep at least 100px visible
            x = max(screen_geo.left() - self.width() + 100,
                    min(x, screen_geo.right() - 100))
            # Clamp y to keep at least 50px visible
            y = max(screen_geo.top(),
                    min(y, screen_geo.bottom() - 50))
        return (x, y)

    def _restore_position(self) -> bool:
        """Restore position from settings.

        Returns:
            True if position was restored, False if using default.
        """
        try:
            position = settings_manager.load_streaming_overlay_position()
            if position:
                x, y = self._validate_position(position['x'], position['y'])
                self.move(x, y)
                self.logger.debug(f"Restored streaming overlay position: {x}, {y}")
                return True
        except Exception as e:
            self.logger.warning(f"Failed to restore streaming overlay position: {e}")
        return False

    def show_overlay(self, state: Optional[str] = None):
        """Show overlay at saved position or screen center.

        Args:
            state: Optional state to set. Defaults to STREAMING.
        """
        # Try to restore saved position, otherwise use screen center
        if not self._restore_position():
            # No saved position - use screen center
            center_pos = self._get_screen_center()
            self.move(center_pos)
            self.logger.debug(f"No saved position, using screen center: {center_pos.x()}, {center_pos.y()}")

        # Set state
        if state is not None:
            self.set_state(state)
        elif self.current_state == self.STATE_IDLE:
            self.set_state(self.STATE_STREAMING)

        # Fade in
        self._fade_animation.stop()
        self._is_fading_out = False
        self._opacity_effect.setOpacity(0.0)
        self._fade_animation.setStartValue(0.0)
        self._fade_animation.setEndValue(1.0)
        self._fade_animation.start()

        self.show()
        self.raise_()
