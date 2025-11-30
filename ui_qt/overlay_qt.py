"""
Modern PyQt6 Waveform Overlay.
Real-time audio visualization overlay with blur effects and animations.
"""
import logging
import math
import random
import time
from typing import Optional, List
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QTimer, QRect, QRectF, pyqtSignal, QPoint
from PyQt6.QtGui import (
    QPainter, QPainterPath, QColor, QBrush, QPen,
    QLinearGradient, QFont, QCursor
)
from config import config
from settings import settings_manager
from ui_qt.waveform_styles import style_factory
from ui_qt.waveform_styles.base_style import BaseWaveformStyle


class STTParticle:
    """Particle for STT enable/disable animations."""

    def __init__(self, x: float, y: float, vx: float, vy: float, hue: float):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.hue = hue
        self.life = 1.0
        self.size = random.uniform(2.0, 4.0)

    def update(self, dt: float, damping: float = 0.98) -> bool:
        """Update particle position and life. Returns True if still alive."""
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.vx *= damping
        self.vy *= damping
        self.life -= dt * 0.5  # Slower decay for longer visibility
        return self.life > 0

    def get_color(self) -> QColor:
        """Get particle color based on hue and life."""
        alpha = int(255 * self.life)
        return QColor.fromHsv(int(self.hue) % 360, 200, 230, alpha)


class ModernWaveformOverlay(QWidget):
    """Modern waveform overlay with smooth animations."""

    state_changed = pyqtSignal(str)

    # States
    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_PROCESSING = "processing"
    STATE_TRANSCRIBING = "transcribing"
    STATE_CANCELING = "canceling"
    STATE_STT_ENABLE = "stt_enable"
    STATE_STT_DISABLE = "stt_disable"
    STATE_COPIED = "copied"
    STATE_LARGE_FILE_SPLITTING = "large_file_splitting"
    STATE_LARGE_FILE_PROCESSING = "large_file_processing"

    def __init__(self):
        """Initialize the overlay."""
        super().__init__()
        self.logger = logging.getLogger(__name__)

        # Window properties
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Set fixed size from config
        self.overlay_width = config.WAVEFORM_OVERLAY_WIDTH
        self.overlay_height = config.WAVEFORM_OVERLAY_HEIGHT
        self.setFixedSize(self.overlay_width, self.overlay_height)

        # State
        self.current_state = self.STATE_IDLE
        self.audio_levels: List[float] = [0.0] * 20
        self.animation_time = 0.0
        self.cancel_progress = 0.0
        self.stt_particles: List[STTParticle] = []

        # Large file information for warning states
        self.large_file_info = {
            'file_size_mb': 0.0,
            'chunk_count': 0
        }

        # Load waveform style
        current_style, style_configs = settings_manager.load_waveform_style_settings()
        try:
            style_config = style_configs.get(current_style, config.WAVEFORM_STYLE_CONFIGS.get('particle', {}))
            self.style: BaseWaveformStyle = style_factory.create_style(
                current_style,
                self.overlay_width,
                self.overlay_height,
                style_config
            )
        except (ValueError, KeyError):
            # Fallback to particle style if loading fails
            self.logger.warning(f"Failed to load style '{current_style}', using particle")
            self.style = style_factory.create_style('particle', self.overlay_width, self.overlay_height)

        # Animation
        self.timer = QTimer()
        self.timer.timeout.connect(self._update_animation)
        self.frame_rate = 30
        self.animation_duration = 0
        self.last_frame_time = time.time()

        # Hide by default
        self.hidden_timer = QTimer()
        self.hidden_timer.setSingleShot(True)
        self.hidden_timer.timeout.connect(self.hide)

    def paintEvent(self, event):
        """Paint the overlay."""
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            # Draw background with blur effect
            self._draw_background(painter)

            # Get drawing rect
            rect = self.rect()

            # Draw state-specific content using style
            if self.current_state == self.STATE_RECORDING:
                if self.style:
                    self.style.draw_recording_state(painter, rect, "Recording...")
            elif self.current_state == self.STATE_PROCESSING:
                if self.style:
                    self.style.draw_processing_state(painter, rect, "Processing...")
            elif self.current_state == self.STATE_TRANSCRIBING:
                if self.style:
                    self.style.draw_transcribing_state(painter, rect, "Transcribing...")
            elif self.current_state == self.STATE_CANCELING:
                if self.style:
                    self.style.draw_canceling_state(painter, rect, "Cancelled")
            elif self.current_state == self.STATE_STT_ENABLE:
                self._draw_stt_enable_state(painter)
            elif self.current_state == self.STATE_STT_DISABLE:
                self._draw_stt_disable_state(painter)
            elif self.current_state == self.STATE_COPIED:
                self._draw_copied_state(painter)
            elif self.current_state == self.STATE_LARGE_FILE_SPLITTING:
                self._draw_large_file_splitting_state(painter)
            elif self.current_state == self.STATE_LARGE_FILE_PROCESSING:
                self._draw_large_file_processing_state(painter)
        except Exception as e:
            # Log error but don't crash the overlay
            self.logger.error(f"Error drawing waveform frame: {e}", exc_info=True)
            # Draw a simple fallback
            try:
                painter = QPainter(self)
                painter.fillRect(self.rect(), QColor(45, 45, 68, 200))
                painter.setPen(QPen(QColor(224, 224, 255)))
                painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Error")
            except Exception:
                pass  # If even fallback fails, just skip

    def _draw_background(self, painter: QPainter):
        """Draw the background with frosted glass effect."""
        rect = self.rect()

        # Draw semi-transparent background
        painter.fillRect(rect, QColor(45, 45, 68, 200))

        # Draw border
        painter.setPen(QPen(QColor(64, 64, 96, 150), 1))
        painter.drawRoundedRect(rect, 12, 12)

    def _draw_recording_state(self, painter: QPainter):
        """Draw recording state visualization."""
        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Draw waveform bars
        bar_count = 20
        bar_width = (w - 40) / bar_count
        start_x = 20

        for i in range(bar_count):
            x = start_x + i * bar_width
            level = self.audio_levels[i] if i < len(self.audio_levels) else 0.0
            bar_height = max(10, level * (h - 40))

            # Color gradient based on level
            if level > 0.7:
                color = QColor(239, 68, 68)  # Red
            elif level > 0.4:
                color = QColor(99, 102, 241)  # Indigo
            else:
                color = QColor(139, 92, 246)  # Purple

            painter.fillRect(
                int(x + bar_width * 0.2),
                int(h / 2 - bar_height / 2),
                int(bar_width * 0.6),
                int(bar_height),
                color
            )

        # Draw status text
        painter.setPen(QPen(QColor(224, 224, 255)))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, "Recording...")

    def _draw_processing_state(self, painter: QPainter):
        """Draw processing state."""
        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Draw rotating spinner
        painter.setPen(QPen(QColor(99, 102, 241), 3))
        spinner_rect = QRect(w // 2 - 20, h // 2 - 20, 40, 40)
        angle = int((self.animation_time * 360) % 360)
        painter.drawArc(spinner_rect, angle * 16, 200 * 16)

        # Status text
        painter.setPen(QPen(QColor(224, 224, 255)))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, "Processing...")

    def _draw_transcribing_state(self, painter: QPainter):
        """Draw transcribing state."""
        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Draw pulsing circles
        progress = (self.animation_time * 2) % 1.0
        for i in range(3):
            offset = (progress + i * 0.3) % 1.0
            radius = int(15 + offset * 15)
            alpha = int(200 * (1 - offset))
            color = QColor(0, 212, 255, alpha)

            painter.setPen(QPen(color, 2))
            painter.drawEllipse(w // 2 - radius, h // 2 - radius, radius * 2, radius * 2)

        # Status text
        painter.setPen(QPen(QColor(224, 224, 255)))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, "Transcribing...")

    def _draw_canceling_state(self, painter: QPainter):
        """Draw canceling state with shrinking X."""
        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Draw shrinking X
        progress = self.cancel_progress  # 0.0 to 1.0
        size = int(40 * (1 - progress * 0.8))

        # X lines
        painter.setPen(QPen(QColor(239, 68, 68), 4))
        painter.drawLine(
            w // 2 - size,
            h // 2 - size,
            w // 2 + size,
            h // 2 + size
        )
        painter.drawLine(
            w // 2 + size,
            h // 2 - size,
            w // 2 - size,
            h // 2 + size
        )

        # Status text
        painter.setPen(QPen(QColor(224, 224, 255)))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, "Canceling...")

    def _draw_stt_enable_state(self, painter: QPainter):
        """Draw STT enable state with power up particle effect."""
        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Draw checkmark first (behind particles) - fades in after particles converge
        if self.animation_time > 0.4:
            progress = min(1.0, (self.animation_time - 0.4) / 0.3)
            alpha = int(200 * progress)
            painter.setPen(QPen(QColor(16, 185, 129, alpha), 3))
            painter.drawLine(int(w // 2 - 15), int(h // 2), int(w // 2 - 5), int(h // 2 + 10))
            painter.drawLine(int(w // 2 - 5), int(h // 2 + 10), int(w // 2 + 15), int(h // 2 - 10))

        # Draw particles on top
        painter.setPen(Qt.PenStyle.NoPen)
        for particle in self.stt_particles:
            color = particle.get_color()
            painter.setBrush(color)
            size = particle.size * particle.life
            painter.drawEllipse(QRectF(
                particle.x - size, particle.y - size,
                size * 2, size * 2
            ))

            # Glow effect for brighter particles
            if particle.life > 0.3:
                glow_color = QColor(color)
                glow_color.setAlpha(100)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(glow_color, 1))
                glow_size = size + 3
                painter.drawEllipse(QRectF(
                    particle.x - glow_size, particle.y - glow_size,
                    glow_size * 2, glow_size * 2
                ))
                painter.setPen(Qt.PenStyle.NoPen)

        # Status text
        painter.setPen(QPen(QColor(224, 224, 255)))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, "Enabled")

    def _draw_stt_disable_state(self, painter: QPainter):
        """Draw STT disable state with power down particle effect."""
        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Draw X first (behind particles) - appears quickly then particles explode from it
        if self.animation_time > 0.1:
            progress = min(1.0, (self.animation_time - 0.1) / 0.2)
            alpha = int(200 * progress)
            x_size = 15
            painter.setPen(QPen(QColor(239, 68, 68, alpha), 3))
            painter.drawLine(w // 2 - x_size, h // 2 - x_size, w // 2 + x_size, h // 2 + x_size)
            painter.drawLine(w // 2 + x_size, h // 2 - x_size, w // 2 - x_size, h // 2 + x_size)

        # Draw particles (exploding outward) on top
        painter.setPen(Qt.PenStyle.NoPen)
        for particle in self.stt_particles:
            color = particle.get_color()
            painter.setBrush(color)
            size = particle.size * particle.life
            painter.drawEllipse(QRectF(
                particle.x - size, particle.y - size,
                size * 2, size * 2
            ))

            # Glow effect for brighter particles
            if particle.life > 0.3:
                glow_color = QColor(color)
                glow_color.setAlpha(100)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(glow_color, 1))
                glow_size = size + 3
                painter.drawEllipse(QRectF(
                    particle.x - glow_size, particle.y - glow_size,
                    glow_size * 2, glow_size * 2
                ))
                painter.setPen(Qt.PenStyle.NoPen)

        # Status text
        painter.setPen(QPen(QColor(224, 224, 255)))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, "Disabled")

    def _draw_copied_state(self, painter: QPainter):
        """Draw copied to clipboard state with sparkle particle effect."""
        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Draw clipboard icon first (behind particles) - fades in after particles converge
        if self.animation_time > 0.3:
            progress = min(1.0, (self.animation_time - 0.3) / 0.3)
            alpha = int(220 * progress)
            
            # Draw a stylized clipboard/document icon
            icon_color = QColor(0, 180, 255, alpha)
            painter.setPen(QPen(icon_color, 2))
            
            # Clipboard body
            cx, cy = w // 2, h // 2 - 5
            painter.drawRoundedRect(cx - 12, cy - 10, 24, 28, 3, 3)
            
            # Clipboard clip at top
            painter.drawRect(cx - 6, cy - 14, 12, 6)
            
            # Lines representing text
            painter.setPen(QPen(icon_color, 1.5))
            painter.drawLine(cx - 7, cy + 2, cx + 7, cy + 2)
            painter.drawLine(cx - 7, cy + 8, cx + 5, cy + 8)

        # Draw particles on top
        painter.setPen(Qt.PenStyle.NoPen)
        for particle in self.stt_particles:
            color = particle.get_color()
            painter.setBrush(color)
            size = particle.size * particle.life
            painter.drawEllipse(QRectF(
                particle.x - size, particle.y - size,
                size * 2, size * 2
            ))

            # Glow effect for brighter particles
            if particle.life > 0.3:
                glow_color = QColor(color)
                glow_color.setAlpha(100)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(glow_color, 1))
                glow_size = size + 3
                painter.drawEllipse(QRectF(
                    particle.x - glow_size, particle.y - glow_size,
                    glow_size * 2, glow_size * 2
                ))
                painter.setPen(Qt.PenStyle.NoPen)

        # Status text
        painter.setPen(QPen(QColor(224, 224, 255)))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, "Copied!")

    def set_large_file_info(self, file_size_mb: float, chunk_count: int = 0):
        """Set information about the large file being processed.

        Args:
            file_size_mb: File size in megabytes.
            chunk_count: Number of chunks (for splitting backends).
        """
        self.large_file_info = {
            'file_size_mb': file_size_mb,
            'chunk_count': chunk_count
        }

    def _draw_large_file_splitting_state(self, painter: QPainter):
        """Draw large file splitting warning (for API backends)."""
        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Animated scissors icon in amber
        progress = (self.animation_time * 2) % 1.0
        center_x, center_y = w // 2, h // 2 - 10

        # Scissors blades animation (opening/closing)
        blade_angle = 12 + 8 * math.sin(progress * math.pi * 2)

        amber = QColor(251, 191, 36)
        painter.setPen(QPen(amber, 3))

        # Draw scissors (two crossing blades)
        # Top blade
        painter.drawLine(
            int(center_x - 18), int(center_y - blade_angle),
            int(center_x + 12), int(center_y + 2)
        )
        # Bottom blade
        painter.drawLine(
            int(center_x - 18), int(center_y + blade_angle),
            int(center_x + 12), int(center_y - 2)
        )
        # Handle circles
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(int(center_x - 24), int(center_y - blade_angle - 5), 10, 10)
        painter.drawEllipse(int(center_x - 24), int(center_y + blade_angle - 5), 10, 10)

        # Status text with file size
        painter.setPen(QPen(amber))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        text = f"Splitting ({self.large_file_info['file_size_mb']:.1f} MB)..."
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, text)

    def _draw_large_file_processing_state(self, painter: QPainter):
        """Draw large file processing info (for local backend)."""
        rect = self.rect()
        w, h = rect.width(), rect.height()

        # Animated timer/clock in cyan
        progress = (self.animation_time * 0.5) % 1.0
        center_x, center_y = w // 2, h // 2 - 10
        radius = 18

        cyan = QColor(0, 212, 255)
        painter.setPen(QPen(cyan, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Clock circle
        painter.drawEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)

        # Clock hands (rotating)
        hand_angle = progress * 2 * math.pi - math.pi / 2
        hand_length = radius - 5
        hand_x = center_x + int(hand_length * math.cos(hand_angle))
        hand_y = center_y + int(hand_length * math.sin(hand_angle))
        painter.drawLine(center_x, center_y, hand_x, hand_y)

        # Short hour hand
        hour_angle = progress * 2 * math.pi / 12 - math.pi / 2
        hour_length = radius - 10
        hour_x = center_x + int(hour_length * math.cos(hour_angle))
        hour_y = center_y + int(hour_length * math.sin(hour_angle))
        painter.setPen(QPen(cyan, 3))
        painter.drawLine(center_x, center_y, hour_x, hour_y)

        # Center dot
        painter.setBrush(QBrush(cyan))
        painter.drawEllipse(center_x - 3, center_y - 3, 6, 6)

        # Status text with file size
        painter.setPen(QPen(cyan))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        text = f"Processing ({self.large_file_info['file_size_mb']:.1f} MB)..."
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, text)

    def _update_animation(self):
        """Update animation time and redraw."""
        # Calculate delta time
        current_time = time.time()
        delta_time = current_time - self.last_frame_time
        self.last_frame_time = current_time

        self.animation_time += delta_time

        # Update style animation
        if self.style:
            self.style.update_animation_time(delta_time)

        if self.current_state == self.STATE_CANCELING:
            self.cancel_progress = min(1.0, self.animation_time / 0.8)
            if self.cancel_progress >= 1.0:
                self.set_state(self.STATE_IDLE)
                self.timer.stop()
        elif self.current_state in [self.STATE_STT_ENABLE, self.STATE_STT_DISABLE, self.STATE_COPIED]:
            # Update particles
            self._update_stt_particles(delta_time)

        self.update()

    def set_state(self, state: str):
        """Set the overlay state."""
        if self.current_state != state:
            self.current_state = state
            self.animation_time = 0.0
            self.cancel_progress = 0.0
            self.last_frame_time = time.time()  # Reset to prevent huge delta on first frame

            # Set canceling start time for style
            if state == self.STATE_CANCELING and self.style:
                self.style.set_canceling_start_time(time.time())

            # Initialize particles for STT and copied states
            if state == self.STATE_STT_ENABLE:
                self._init_power_up_particles()
            elif state == self.STATE_STT_DISABLE:
                self._init_power_down_particles()
            elif state == self.STATE_COPIED:
                self._init_copied_particles()
            else:
                self.stt_particles = []

            if state == self.STATE_IDLE:
                self.timer.stop()
            else:
                self.timer.start(1000 // self.frame_rate)

            self.state_changed.emit(state)
            self.logger.debug(f"Overlay state changed to: {state}")

            # Auto-hide after delay for certain states
            if state in [self.STATE_STT_ENABLE, self.STATE_STT_DISABLE, self.STATE_COPIED]:
                self.hidden_timer.start(1500)

    def _init_power_up_particles(self):
        """Initialize particles for power up animation - converging to center."""
        self.stt_particles = []
        center_x = self.overlay_width // 2
        center_y = self.overlay_height // 2 - 5

        # Create particles around the edges that will converge to center
        for i in range(60):
            # Spawn from random positions around edges
            angle = (i / 60) * 2 * math.pi + random.uniform(-0.3, 0.3)
            radius = random.uniform(50, 90)

            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)

            # Velocity towards center with some swirl
            speed = random.uniform(60, 100)
            vx = -math.cos(angle) * speed + random.uniform(-15, 15)
            vy = -math.sin(angle) * speed + random.uniform(-15, 15)

            # Green/cyan hues for power up
            hue = random.uniform(120, 180)

            particle = STTParticle(x, y, vx, vy, hue)
            particle.size = random.uniform(3.0, 6.0)  # Larger particles
            self.stt_particles.append(particle)

    def _init_power_down_particles(self):
        """Initialize particles for power down animation - exploding from center."""
        self.stt_particles = []
        center_x = self.overlay_width // 2
        center_y = self.overlay_height // 2 - 5

        # Create particles at center that will explode outward
        for i in range(60):
            # Start near center
            x = center_x + random.uniform(-8, 8)
            y = center_y + random.uniform(-8, 8)

            # Velocity outward in all directions
            angle = (i / 60) * 2 * math.pi + random.uniform(-0.3, 0.3)
            speed = random.uniform(100, 200)
            vx = math.cos(angle) * speed
            vy = math.sin(angle) * speed

            # Red/orange hues for power down
            hue = random.uniform(0, 40)

            particle = STTParticle(x, y, vx, vy, hue)
            particle.size = random.uniform(3.0, 6.0)  # Larger particles
            self.stt_particles.append(particle)

    def _init_copied_particles(self):
        """Initialize particles for copied animation - sparkle converging effect."""
        self.stt_particles = []
        center_x = self.overlay_width // 2
        center_y = self.overlay_height // 2 - 5

        # Create particles around the edges that will converge to center with sparkle effect
        for i in range(50):
            # Spawn from random positions around edges
            angle = (i / 50) * 2 * math.pi + random.uniform(-0.3, 0.3)
            radius = random.uniform(45, 80)

            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)

            # Velocity towards center with slight swirl
            speed = random.uniform(50, 90)
            vx = -math.cos(angle) * speed + random.uniform(-10, 10)
            vy = -math.sin(angle) * speed + random.uniform(-10, 10)

            # Cyan/blue hues for copy action
            hue = random.uniform(180, 220)

            particle = STTParticle(x, y, vx, vy, hue)
            particle.size = random.uniform(2.5, 5.0)
            self.stt_particles.append(particle)

    def _update_stt_particles(self, dt: float):
        """Update STT particle positions and apply forces."""
        center_x = self.overlay_width // 2
        center_y = self.overlay_height // 2 - 5

        alive_particles = []
        for particle in self.stt_particles:
            if self.current_state in [self.STATE_STT_ENABLE, self.STATE_COPIED]:
                # Power up / Copy: attract to center with swirl
                dx = center_x - particle.x
                dy = center_y - particle.y
                distance = math.sqrt(dx * dx + dy * dy)

                if distance > 3:
                    # Normalize
                    nx = dx / distance
                    ny = dy / distance

                    # Strong attraction + swirl
                    attraction = 800 / (distance + 5)
                    swirl = 200 if self.current_state == self.STATE_STT_ENABLE else 150

                    particle.vx += (nx * attraction - ny * swirl) * dt
                    particle.vy += (ny * attraction + nx * swirl) * dt
                else:
                    # At center, fade fast
                    particle.life -= dt * 3.0

            # Update physics
            if particle.update(dt, damping=0.92):
                alive_particles.append(particle)

        self.stt_particles = alive_particles

    def update_audio_levels(self, levels: List[float]):
        """Update audio level data."""
        self.audio_levels = levels[:20]  # Keep only 20 levels

        # Update style with audio levels
        if self.style:
            current_level = sum(levels) / len(levels) if levels else 0.0
            self.style.update_audio_levels(self.audio_levels, current_level)

    def hide(self):
        """Hide the overlay and stop animations."""
        # Stop animation timer
        self.timer.stop()
        self.hidden_timer.stop()

        # Reset state to IDLE to prevent hanging
        self.current_state = self.STATE_IDLE
        self.animation_time = 0.0
        self.cancel_progress = 0.0

        super().hide()

    def show_at_cursor(self, state: Optional[str] = None):
        """Show overlay near the cursor with optional state.

        Args:
            state: Optional state to set. If None, uses current state or RECORDING as default.
        """
        # Get global cursor position
        cursor_pos = QCursor.pos()

        # Position overlay near cursor (offset slightly)
        x = cursor_pos.x() + 10
        y = cursor_pos.y() + 10

        self.move(x, y)
        self.show()

        # Set state if provided, otherwise default to RECORDING
        if state is not None:
            self.set_state(state)
        elif self.current_state == self.STATE_IDLE:
            self.set_state(self.STATE_RECORDING)

    def closeEvent(self, event):
        """Handle closing."""
        self.timer.stop()
        self.hidden_timer.stop()
        event.accept()
