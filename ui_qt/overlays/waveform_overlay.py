import logging
import math
import random
import sys
import time
from typing import Optional, List
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import Qt, QTimer, QRect, QRectF, pyqtSignal, QPoint, QPointF
from PyQt6.QtGui import (
    QPainter, QPainterPath, QColor, QPen,
    QFont, QFontMetrics, QCursor, QTextLayout
)
from config import config
from services.settings import resolve_streaming_overlay_font_size, settings_manager
from ui_qt.utils.overlay_position import (
    max_height_for_anchor,
    preferred_overlay_position,
)
from ui_qt.utils.palette import token_color
from ui_qt.waveform_styles import Particle, ParticleStyle, round_pen

logger = logging.getLogger(__name__)

CAPTION_MS = 2500
_LANGUAGE_FLASH_S = 0.6
_BADGE_HEIGHT = 18
_BADGE_MARGIN = 9


def _platform_takes_overlay_clicks() -> bool:
    """Where a click on a non-activating window leaves the target app focused.

    Windows honours WS_EX_NOACTIVATE and X11 the input hint; on macOS the Tool
    window behaviour is unverified, so the chip there is display-only.
    """
    return QApplication.platformName() in ("windows", "xcb")


class WaveformOverlay(QWidget):
    state_changed = pyqtSignal(str)
    #: The language chip was clicked; only standalone Windows and X11 overlays
    #: take clicks, so the app being dictated into keeps focus.
    language_cycle_requested = pyqtSignal()

    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_STREAMING = "streaming"
    STATE_COMMAND_LISTENING = "command_listening"
    STATE_PROCESSING = "processing"
    STATE_TRANSCRIBING = "transcribing"
    STATE_CLEANING = "cleaning"
    STATE_REWRITING = "rewriting"
    STATE_CANCELING = "canceling"
    STATE_STT_ENABLE = "stt_enable"
    STATE_STT_DISABLE = "stt_disable"
    STATE_COPIED = "copied"
    STATE_LANGUAGE = "language"
    STATE_LARGE_FILE_SPLITTING = "large_file_splitting"

    LISTENING_STATES = frozenset((STATE_RECORDING, STATE_STREAMING, STATE_COMMAND_LISTENING))
    _PREVIEW_STATES = frozenset((STATE_STREAMING, STATE_COMMAND_LISTENING))
    _TRANSIENT_STATES = frozenset((STATE_STT_ENABLE, STATE_STT_DISABLE, STATE_COPIED, STATE_LANGUAGE))
    # ParticleStyle simulates only its own states; the command and rewrite
    # looks borrow theirs.
    _STYLE_STATES = {STATE_COMMAND_LISTENING: STATE_RECORDING, STATE_REWRITING: STATE_CLEANING}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._embedded = parent is not None

        self.setWindowFlags(
            Qt.WindowType.Widget if self._embedded else
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, not self._embedded)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if self._embedded:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            parent.installEventFilter(self)
        else:
            # The paste goes to whichever app has focus, so neither showing nor
            # clicking the overlay may take it (WS_EX_NOACTIVATE on Windows).
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setMouseTracking(True)
        if sys.platform == "darwin":
            # On macOS, Qt Tool windows are hidden whenever the app is not the
            # frontmost application (or when its main window is minimized). During
            # dictation the user is typically working in another app, so without
            # this the overlay disappears. Force it to stay visible regardless.
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)

        self.overlay_width = config.WAVEFORM_OVERLAY_WIDTH
        self.overlay_height = config.WAVEFORM_OVERLAY_HEIGHT
        self._base_height = self.overlay_height
        self._streaming_max_height = getattr(
            config, "WAVEFORM_STREAMING_MAX_HEIGHT", 200
        )
        self.setFixedSize(self.overlay_width, self.overlay_height)

        self.current_state = self.STATE_IDLE
        self.audio_levels: List[float] = [0.0] * 20
        self.animation_time = 0.0
        self.cancel_progress = 0.0
        self.stt_particles: List[Particle] = []
        self._streaming_preview_text: str = ""
        self._streaming_font_size = resolve_streaming_overlay_font_size()
        # Cursor anchor used to keep the overlay on-screen as it grows.
        self._anchor_pos: Optional[QPoint] = None

        self.large_file_size_mb = 0.0

        style_config = config.WAVEFORM_STYLE_CONFIGS.get('particle', {})
        self.style = ParticleStyle(
            self.overlay_width, self.overlay_height, style_config
        )

        self.timer = QTimer()
        self.timer.timeout.connect(self._update_animation)
        self.frame_rate = config.WAVEFORM_FRAME_RATE
        self.animation_duration = 0
        self.last_frame_time = time.monotonic()

        self.hidden_timer = QTimer()
        self.hidden_timer.setSingleShot(True)
        self.hidden_timer.timeout.connect(self.hide)

        self._hands_free = False
        self._language = ""
        self._language_choices: tuple = ()
        self._language_changed_at: Optional[float] = None
        self._chip_rect = QRectF()
        self._chip_hover = False
        self._caption = ""
        self._caption_started = 0.0
        self._caption_timer = QTimer(self)
        self._caption_timer.setSingleShot(True)
        self._caption_timer.timeout.connect(self._clear_caption)
        if self._embedded:
            self.hide()

    def paintEvent(self, event):
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            self._draw_background(painter)

            rect = self.rect()

            if self.current_state == self.STATE_RECORDING:
                self.style.draw_recording_state(painter, rect, "" if self._caption else "Recording...")
            elif self.current_state == self.STATE_STREAMING:
                self._draw_streaming_state(painter, rect)
            elif self.current_state == self.STATE_COMMAND_LISTENING:
                self._draw_streaming_state(painter, rect, "Listening for an edit...", "accent-soft")
            elif self.current_state == self.STATE_PROCESSING:
                self.style.draw_processing_state(painter, rect, "Processing...")
            elif self.current_state == self.STATE_TRANSCRIBING:
                self.style.draw_transcribing_state(painter, rect, "Transcribing...")
            elif self.current_state == self.STATE_CLEANING:
                self._draw_cleaning_state(painter)
            elif self.current_state == self.STATE_REWRITING:
                self._draw_cleaning_state(painter, "Rewriting...")
            elif self.current_state == self.STATE_CANCELING:
                self.style.draw_canceling_state(painter, rect, "Canceled")
            elif self.current_state == self.STATE_STT_ENABLE:
                self._draw_stt_enable_state(painter)
            elif self.current_state == self.STATE_STT_DISABLE:
                self._draw_stt_disable_state(painter)
            elif self.current_state == self.STATE_COPIED:
                self._draw_copied_state(painter)
            elif self.current_state == self.STATE_LANGUAGE:
                self._draw_language_state(painter)
            elif self.current_state == self.STATE_LARGE_FILE_SPLITTING:
                self._draw_large_file_splitting_state(painter)
            self._draw_listening_extras(painter)
        except Exception as e:
            logger.error(f"Error drawing waveform frame: {e}", exc_info=True)
            try:
                painter = QPainter(self)
                painter.fillRect(self.rect(), token_color("overlay-bg"))
                painter.setPen(QPen(token_color("overlay-text")))
                painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Error")
            except Exception:
                pass

    def _draw_streaming_state(self, painter: QPainter, rect: QRect,
                              status: str = "Listening...", status_token: str = "overlay-text"):
        """Draw recording particles plus live preview text near the cursor.

        Args:
            painter: Active painter for this frame.
            rect: Full overlay bounds.
            status: Shown in the band until preview text arrives.
            status_token: Palette role for the status.
        """
        particle_height = min(self._base_height, rect.height())
        particle_rect = QRect(0, 0, rect.width(), particle_height)
        # Keep particle physics in the compact recording band even when the
        # overlay grows to fit preview text.
        previous_height = self.style.height
        self.style.height = self._base_height
        try:
            self.style.draw_recording_state(painter, particle_rect, "")
        finally:
            self.style.height = previous_height
        if not self._streaming_preview_text and not self._caption:
            self._draw_status(painter, status, token_color(status_token))

        if self._streaming_preview_text:
            self._draw_streaming_preview_text(painter, rect)

    def _status_rect(self) -> QRect:
        return QRect(0, self._base_height - 25, self.width(), 20)

    def _draw_status(self, painter: QPainter, text: str, color: QColor) -> None:
        painter.setPen(QPen(color))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        rect = self._status_rect().adjusted(12, 0, -12, 0)
        text = QFontMetrics(painter.font()).elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _streaming_preview_font(self) -> QFont:
        return QFont("Segoe UI", self._streaming_font_size)

    def refresh_streaming_font_size(self):
        """Reload preview font size from settings and reflow if needed."""
        new_size = resolve_streaming_overlay_font_size()
        if new_size == self._streaming_font_size:
            return
        self._streaming_font_size = new_size
        if self._streaming_preview_text:
            self._apply_streaming_height()
            self.update()

    def _draw_streaming_preview_text(self, painter: QPainter, rect: QRect):
        """Draw wrapped streaming preview text under the particle band.

        Aligns to the bottom so the newest words stay visible when the text is
        taller than the overlay's height cap.
        """
        top = self._base_height - 8
        text_rect = QRect(10, top, rect.width() - 20, max(20, rect.height() - top - 8))
        painter.setPen(QPen(token_color("overlay-text")))
        painter.setFont(self._streaming_preview_font())
        if self._embedded:
            key = (self._streaming_preview_text, text_rect.width(), self._streaming_font_size)
            if getattr(self, "_preview_layout_key", None) != key:
                self._preview_layout_key = key
                self._preview_layout = QTextLayout(key[0], self._streaming_preview_font())
                self._preview_lines = []
                self._preview_layout.beginLayout()
                while True:
                    line = self._preview_layout.createLine()
                    if not line.isValid():
                        break
                    line.setLineWidth(text_rect.width())
                    self._preview_lines.append(line)
                self._preview_layout.endLayout()
            visible, height = [], 0
            for line in reversed(self._preview_lines):
                if height + line.height() > text_rect.height():
                    break
                visible.append(line)
                height += line.height()
            y = text_rect.bottom() - height + 1
            for line in reversed(visible):
                line.draw(painter, QPointF(text_rect.left(), y))
                y += line.height()
            return
        painter.drawText(
            text_rect,
            int(
                Qt.AlignmentFlag.AlignLeft
                | Qt.AlignmentFlag.AlignBottom
                | Qt.TextFlag.TextWordWrap
            ),
            self._streaming_preview_text,
        )

    def clear_streaming_text(self):
        self._streaming_preview_text = ""
        self._apply_streaming_height()

    def update_streaming_text(self, text: str, is_final: bool = True):
        """Update live preview text shown during streaming recording.

        Args:
            text: Full preview transcript so far.
            is_final: Unused; kept for API compatibility with prior overlay.
        """
        self._streaming_preview_text = (text or "").strip()
        self._apply_streaming_height()
        self.update()

    def _available_geometry_for_anchor(self) -> Optional[QRect]:
        point = self._anchor_pos if self._anchor_pos is not None else QCursor.pos()
        screen = QApplication.screenAt(point)
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return None
        return screen.availableGeometry()

    def _reposition_near_anchor(self):
        """Move the overlay near its anchor while keeping it fully on-screen."""
        if self._embedded:
            parent = self.parentWidget()
            self.move(max(0, parent.width() - self.width() - 16), max(0, parent.height() - self.height() - 64))
            return
        if self._anchor_pos is None:
            return
        available = self._available_geometry_for_anchor()
        if available is None:
            self.move(self._anchor_pos.x() + 10, self._anchor_pos.y() + 10)
            return
        x, y = preferred_overlay_position(
            self._anchor_pos,
            self.overlay_width,
            self.overlay_height,
            available,
        )
        self.move(x, y)

    def _effective_streaming_max_height(self) -> int:
        """Soft config max, further limited by free space near the anchor."""
        if self._embedded:
            return min(self._streaming_max_height, max(self._base_height, self.parentWidget().height() // 3))
        available = self._available_geometry_for_anchor()
        if available is None or self._anchor_pos is None:
            return self._streaming_max_height
        return max_height_for_anchor(
            self._anchor_pos,
            available,
            self._streaming_max_height,
        )

    def _apply_streaming_height(self):
        """Grow or shrink the overlay to fit preview text while streaming."""
        if self.current_state not in self._PREVIEW_STATES and not self._streaming_preview_text:
            if self.height() != self._base_height:
                self.overlay_height = self._base_height
                self.setFixedSize(self.overlay_width, self.overlay_height)
                self._reposition_near_anchor()
            return

        if not self._streaming_preview_text:
            target_height = self._base_height
        else:
            effective_max = self._effective_streaming_max_height()
            font = self._streaming_preview_font()
            metrics_rect = QRect(0, 0, self.overlay_width - 20, effective_max)
            fm = QFontMetrics(font)
            bounded = fm.boundingRect(
                metrics_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap),
                self._streaming_preview_text,
            )
            text_height = bounded.height() + 16
            target_height = min(
                effective_max,
                max(self._base_height, self._base_height - 8 + text_height),
            )

        if target_height != self.overlay_height:
            self.overlay_height = target_height
            self.setFixedSize(self.overlay_width, self.overlay_height)
            self._reposition_near_anchor()

    def _draw_background(self, painter: QPainter):
        command = self.current_state == self.STATE_COMMAND_LISTENING
        if self._embedded:
            painter.fillRect(self.rect(), token_color("bg"))
            painter.setPen(QPen(token_color("accent") if command else token_color("border"), 1))
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
            return
        # Inset by half the pen width so the 1px border isn't clipped.
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)

        painter.fillPath(path, token_color("overlay-bg"))
        painter.setPen(QPen(token_color("overlay-border"), 1))
        painter.drawPath(path)
        if command:
            # Command Mode is a recording that edits text; the accent ring
            # tells it apart from dictation at a glance.
            ring = QPainterPath()
            ring.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 11.5, 11.5)
            painter.setPen(QPen(token_color("accent", 210), 1.6))
            painter.drawPath(ring)

    def _draw_particle_swarm(self, painter: QPainter):
        painter.setPen(Qt.PenStyle.NoPen)
        for particle in self.stt_particles:
            color = particle.get_fading_color()
            painter.setBrush(color)
            size = particle.size * particle.life
            painter.drawEllipse(QRectF(
                particle.x - size, particle.y - size,
                size * 2, size * 2
            ))

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

    def _draw_stt_enable_state(self, painter: QPainter):
        rect = self.rect()
        w, h = rect.width(), rect.height()

        if self.animation_time > 0.4:
            progress = min(1.0, (self.animation_time - 0.4) / 0.3)
            alpha = int(200 * progress)
            painter.setPen(round_pen(token_color("success", alpha), 3))
            painter.drawLine(int(w // 2 - 15), int(h // 2), int(w // 2 - 5), int(h // 2 + 10))
            painter.drawLine(int(w // 2 - 5), int(h // 2 + 10), int(w // 2 + 15), int(h // 2 - 10))

        self._draw_particle_swarm(painter)

        painter.setPen(QPen(token_color("overlay-text")))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, "Enabled")

    def _draw_stt_disable_state(self, painter: QPainter):
        rect = self.rect()
        w, h = rect.width(), rect.height()

        if self.animation_time > 0.1:
            progress = min(1.0, (self.animation_time - 0.1) / 0.2)
            alpha = int(200 * progress)
            x_size = 15
            painter.setPen(round_pen(token_color("danger", alpha), 3))
            painter.drawLine(w // 2 - x_size, h // 2 - x_size, w // 2 + x_size, h // 2 + x_size)
            painter.drawLine(w // 2 + x_size, h // 2 - x_size, w // 2 - x_size, h // 2 + x_size)

        self._draw_particle_swarm(painter)

        painter.setPen(QPen(token_color("overlay-text")))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, "Disabled")

    def _draw_copied_state(self, painter: QPainter):
        rect = self.rect()
        w, h = rect.width(), rect.height()

        if self.animation_time > 0.3:
            progress = min(1.0, (self.animation_time - 0.3) / 0.3)
            alpha = int(220 * progress)

            icon_color = token_color("accent-cyan", alpha)
            painter.setPen(round_pen(icon_color, 2))

            cx, cy = w // 2, h // 2 - 5
            painter.drawRoundedRect(cx - 12, cy - 10, 24, 28, 3, 3)

            painter.drawRect(cx - 6, cy - 14, 12, 6)

            painter.setPen(round_pen(icon_color, 1.5))
            painter.drawLine(cx - 7, cy + 2, cx + 7, cy + 2)
            painter.drawLine(cx - 7, cy + 8, cx + 5, cy + 8)

        self._draw_particle_swarm(painter)

        painter.setPen(QPen(token_color("overlay-text")))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, "Copied!")

    def _draw_cleaning_state(self, painter: QPainter, text: str = "Cleaning up..."):
        rect = self.rect()
        w, h = rect.width(), rect.height()
        purple = token_color("purple")

        # Sparkle layout: (x_frac, y_frac, base_size, twinkle_phase). Phases are
        # staggered so the sparkles shimmer in sequence rather than in unison.
        sparkles = (
            (0.50, 0.42, 11.0, 0.0),
            (0.37, 0.28, 6.0, 1.3),
            (0.64, 0.30, 7.5, 2.6),
            (0.41, 0.58, 5.0, 3.9),
            (0.61, 0.55, 6.5, 5.2),
        )
        for x_frac, y_frac, base_size, phase in sparkles:
            twinkle = 0.5 + 0.5 * math.sin(self.animation_time * 3.0 + phase)
            color = QColor(purple)
            color.setAlpha(int(80 + 175 * twinkle))
            self._draw_sparkle(
                painter,
                x_frac * w,
                y_frac * h - 4,
                base_size * (0.55 + 0.45 * twinkle),
                color,
            )

        painter.setPen(QPen(purple))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(
            rect.adjusted(0, h - 25, 0, 0),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )

    @staticmethod
    def _draw_sparkle(painter: QPainter, cx: float, cy: float, size: float, color: QColor):
        painter.setPen(round_pen(color, 2))
        painter.drawLine(int(cx), int(cy - size), int(cx), int(cy + size))
        painter.drawLine(int(cx - size), int(cy), int(cx + size), int(cy))

        accent = QColor(color)
        accent.setAlpha(int(color.alpha() * 0.55))
        diag = size * 0.45
        painter.setPen(round_pen(accent, 1.5))
        painter.drawLine(int(cx - diag), int(cy - diag), int(cx + diag), int(cy + diag))
        painter.drawLine(int(cx - diag), int(cy + diag), int(cx + diag), int(cy - diag))

    def _badge_radius(self) -> float:
        return 2.0 if self._embedded else _BADGE_HEIGHT / 2

    def _draw_listening_extras(self, painter: QPainter) -> None:
        """The hands-free or Command Mode badge, the language chip and a caption."""
        if self.current_state not in self.LISTENING_STATES:
            self._chip_rect = QRectF()
            return
        if self._hands_free or self.current_state == self.STATE_COMMAND_LISTENING:
            self._draw_mode_badge(painter)
        self._chip_rect = self._draw_language_chip(painter) if self._chip_visible() else QRectF()
        if self._caption:
            self._draw_caption(painter)

    def _draw_mode_badge(self, painter: QPainter) -> None:
        command = self.current_state == self.STATE_COMMAND_LISTENING
        text = "Command Mode" if command else "Hands-free"
        font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
        lock_width = 8 if self._hands_free else 0
        width = QFontMetrics(font).horizontalAdvance(text) + 18 + (lock_width + 5 if lock_width else 0)
        rect = QRectF(_BADGE_MARGIN, _BADGE_MARGIN, width, _BADGE_HEIGHT)
        radius = self._badge_radius()
        ink = token_color("accent-soft")
        painter.setPen(Qt.PenStyle.NoPen)
        # Opaque first so particles drifting behind never show through the text.
        painter.setBrush(token_color("overlay-bg"))
        painter.drawRoundedRect(rect, radius, radius)
        painter.setBrush(token_color("accent", 46))
        painter.drawRoundedRect(rect, radius, radius)
        x = rect.left() + 9
        if self._hands_free:
            self._draw_lock(painter, x, rect.center().y(), ink)
            x += lock_width + 5
        painter.setPen(QPen(ink))
        painter.setFont(font)
        painter.drawText(QRectF(x, rect.top(), rect.right() - x, rect.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)

    @staticmethod
    def _draw_lock(painter: QPainter, x: float, cy: float, color: QColor) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(x, cy - 1.0, 8.0, 6.0), 1.5, 1.5)
        shackle = QPainterPath()
        shackle.moveTo(x + 1.9, cy - 1.0)
        shackle.lineTo(x + 1.9, cy - 3.0)
        shackle.arcTo(QRectF(x + 1.9, cy - 5.9, 4.2, 5.8), 180, -180)
        shackle.lineTo(x + 6.1, cy - 1.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(round_pen(color, 1.4))
        painter.drawPath(shackle)

    def _language_text(self) -> str:
        from services.dictation_language import short_label

        return short_label(self._language)

    def _language_name(self) -> str:
        from services.dictation_language import label

        return label(self._language)

    def _language_flash(self) -> float:
        if self._language_changed_at is None:
            return 0.0
        return max(0.0, 1.0 - (time.monotonic() - self._language_changed_at) / _LANGUAGE_FLASH_S)

    def _draw_language_chip(self, painter: QPainter) -> QRectF:
        font = QFont("Segoe UI", 8, QFont.Weight.Bold)
        text = self._language_text()
        width = max(30, QFontMetrics(font).horizontalAdvance(text) + 18)
        rect = QRectF(self.width() - _BADGE_MARGIN - width, _BADGE_MARGIN, width, _BADGE_HEIGHT)
        radius = self._badge_radius()
        flash = self._language_flash()
        hover = self._chip_hover and self._chip_clickable()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(token_color("overlay-bg"))
        painter.drawRoundedRect(rect, radius, radius)
        painter.setBrush(token_color("overlay-rgb", 38 if hover else 22))
        painter.drawRoundedRect(rect, radius, radius)
        if flash:
            painter.setBrush(token_color("accent", int(170 * flash)))
            painter.drawRoundedRect(rect, radius, radius)
        edge = token_color("accent", 220) if hover or flash else token_color("overlay-border")
        painter.setPen(QPen(edge, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.setPen(QPen(token_color("on-accent") if flash > 0.5 else token_color("overlay-text")))
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        return rect

    def _draw_caption(self, painter: QPainter) -> None:
        elapsed = time.monotonic() - self._caption_started
        remaining = CAPTION_MS / 1000 - elapsed
        fade = max(0.0, min(1.0, elapsed / 0.15, remaining / 0.35))
        self._draw_status(painter, self._caption, token_color("warning-text", int(255 * fade)))

    def _draw_language_state(self, painter: QPainter) -> None:
        """The language shortcut's notice: the new language, popping in."""
        grow = min(1.0, self.animation_time / 0.22)
        scale = 0.7 + 0.3 * (1.0 - (1.0 - grow) ** 3)
        font = QFont("Segoe UI", 13, QFont.Weight.Bold)
        text = self._language_text()
        width = max(54, QFontMetrics(font).horizontalAdvance(text) + 28) * scale
        height = 30 * scale
        rect = QRectF((self.width() - width) / 2, 30 - height / 2, width, height)
        radius = 3.0 if self._embedded else height / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(token_color("accent", int(235 * grow)))
        painter.drawRoundedRect(rect, radius, radius)
        font.setPointSizeF(13 * scale)
        painter.setFont(font)
        painter.setPen(QPen(token_color("on-accent", int(255 * grow))))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        self._draw_status(painter, self._language_name(), token_color("overlay-text"))

    def _chip_visible(self) -> bool:
        return bool(self._language) and len(self._language_choices) >= 2

    def _chip_clickable(self) -> bool:
        return (
            not self._embedded
            and _platform_takes_overlay_clicks()
            and self.current_state in self.LISTENING_STATES
            and self._chip_visible()
        )

    def _chip_hit(self, position: QPointF) -> bool:
        return self._chip_clickable() and self._chip_rect.adjusted(-4, -4, 4, 4).contains(position)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._chip_hit(event.position()):
            event.accept()
            self.language_cycle_requested.emit()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self._set_chip_hover(self._chip_hit(event.position()))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._set_chip_hover(False)
        super().leaveEvent(event)

    def _set_chip_hover(self, hover: bool) -> None:
        if hover == self._chip_hover:
            return
        self._chip_hover = hover
        if hover:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()
        self.update()

    def set_large_file_info(self, file_size_mb: float):
        self.large_file_size_mb = file_size_mb

    def _draw_large_file_splitting_state(self, painter: QPainter):
        rect = self.rect()
        w, h = rect.width(), rect.height()

        progress = (self.animation_time * 2) % 1.0
        center_x, center_y = w // 2, h // 2 - 10

        blade_angle = 12 + 8 * math.sin(progress * math.pi * 2)

        amber = token_color("warning")
        painter.setPen(round_pen(amber, 3))

        painter.drawLine(
            int(center_x - 18), int(center_y - blade_angle),
            int(center_x + 12), int(center_y + 2)
        )
        painter.drawLine(
            int(center_x - 18), int(center_y + blade_angle),
            int(center_x + 12), int(center_y - 2)
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(int(center_x - 24), int(center_y - blade_angle - 5), 10, 10)
        painter.drawEllipse(int(center_x - 24), int(center_y + blade_angle - 5), 10, 10)

        painter.setPen(QPen(amber))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        text = f"Splitting ({self.large_file_size_mb:.1f} MB)..."
        painter.drawText(rect.adjusted(0, h - 25, 0, 0), Qt.AlignmentFlag.AlignCenter, text)

    def _update_animation(self):
        current_time = time.monotonic()
        delta_time = max(0.0, min(0.1, current_time - self.last_frame_time))
        self.last_frame_time = current_time

        self.animation_time += delta_time

        self.style.advance(self._STYLE_STATES.get(self.current_state, self.current_state), delta_time)

        if self.current_state == self.STATE_CANCELING:
            self.cancel_progress = min(1.0, self.animation_time / 0.8)
            if self.cancel_progress >= 1.0:
                self.set_state(self.STATE_IDLE)
                self.timer.stop()
        elif self.current_state in [self.STATE_STT_ENABLE, self.STATE_STT_DISABLE, self.STATE_COPIED]:
            self._update_stt_particles(delta_time)

        self.update()

    def set_state(self, state: str):
        if self.current_state != state:
            was_listening = self.current_state in self.LISTENING_STATES
            self.current_state = state
            self.animation_time = 0.0
            self.cancel_progress = 0.0
            self.last_frame_time = time.monotonic()  # Reset to prevent huge delta on first frame

            if state == self.STATE_CANCELING:
                self.style.set_canceling_start_time(time.time())

            if state == self.STATE_STT_ENABLE:
                self._init_particles(
                    count=60, hue_range=(120, 180), mode='converge',
                    speed_range=(60, 100), size_range=(3.0, 6.0),
                    edge_radius=(50, 90), velocity_jitter=15.0,
                )
            elif state == self.STATE_STT_DISABLE:
                self._init_particles(
                    count=60, hue_range=(0, 40), mode='explode',
                    speed_range=(100, 200), size_range=(3.0, 6.0),
                    center_jitter=8.0,
                )
            elif state == self.STATE_COPIED:
                self._init_particles(
                    count=50, hue_range=(180, 220), mode='converge',
                    speed_range=(50, 90), size_range=(2.5, 5.0),
                    edge_radius=(45, 80), velocity_jitter=10.0,
                )
            else:
                self.stt_particles = []

            if state == self.STATE_IDLE:
                self.timer.stop()
            else:
                self.timer.start(1000 // self.frame_rate)

            if state not in self._PREVIEW_STATES:
                self._streaming_preview_text = ""
                if self.overlay_height != self._base_height:
                    self.overlay_height = self._base_height
                    self.setFixedSize(self.overlay_width, self.overlay_height)

            if state not in self.LISTENING_STATES:
                self._end_listening_extras()
            elif not was_listening:
                # A new recording: no latch yet, and the language as saved now.
                self._hands_free = False
                self._refresh_language()

            self.state_changed.emit(state)
            logger.debug(f"Overlay state changed to: {state}")

            if state in self._TRANSIENT_STATES:
                self.hidden_timer.start(config.OVERLAY_HIDE_DELAY_MS)
            else:
                # A recording that starts during a short notice (a language
                # switch just before dictating) must not be hidden by it.
                self.hidden_timer.stop()

    def _init_particles(
        self,
        count: int,
        hue_range: tuple,
        mode: str,
        speed_range: tuple,
        size_range: tuple,
        edge_radius: tuple = (50, 90),
        velocity_jitter: float = 15.0,
        center_jitter: float = 8.0,
    ):
        """Initialize STT particles in either a converging or exploding pattern.

        Args:
            count: Number of particles to spawn.
            hue_range: (min, max) HSV hue for particle color.
            mode: 'converge' (spawn at edges, fly inward) or 'explode' (spawn near
                center, fly outward).
            speed_range: (min, max) particle speed.
            size_range: (min, max) particle radius.
            edge_radius: 'converge' only — (min, max) spawn distance from center.
            velocity_jitter: 'converge' only — random vx/vy noise added per particle.
            center_jitter: 'explode' only — half-width of the random spawn box around center.
        """
        self.stt_particles = []
        center_x = self.overlay_width // 2
        center_y = self.overlay_height // 2 - 5

        for i in range(count):
            angle = (i / count) * 2 * math.pi + random.uniform(-0.3, 0.3)
            speed = random.uniform(*speed_range)
            hue = random.uniform(*hue_range)

            if mode == 'converge':
                radius = random.uniform(*edge_radius)
                x = center_x + radius * math.cos(angle)
                y = center_y + radius * math.sin(angle)
                vx = -math.cos(angle) * speed + random.uniform(-velocity_jitter, velocity_jitter)
                vy = -math.sin(angle) * speed + random.uniform(-velocity_jitter, velocity_jitter)
            else:  # 'explode'
                x = center_x + random.uniform(-center_jitter, center_jitter)
                y = center_y + random.uniform(-center_jitter, center_jitter)
                vx = math.cos(angle) * speed
                vy = math.sin(angle) * speed

            particle = Particle(x, y, vx, vy, hue=hue)
            particle.size = random.uniform(*size_range)
            self.stt_particles.append(particle)

    def _update_stt_particles(self, dt: float):
        center_x = self.overlay_width // 2
        center_y = self.overlay_height // 2 - 5

        alive_particles = []
        for particle in self.stt_particles:
            if self.current_state in [self.STATE_STT_ENABLE, self.STATE_COPIED]:
                dx = center_x - particle.x
                dy = center_y - particle.y
                distance = math.sqrt(dx * dx + dy * dy)

                if distance > 3:
                    nx = dx / distance
                    ny = dy / distance

                    attraction = 800 / (distance + 5)
                    swirl = 200 if self.current_state == self.STATE_STT_ENABLE else 150

                    particle.vx += (nx * attraction - ny * swirl) * dt
                    particle.vy += (ny * attraction + nx * swirl) * dt
                else:
                    particle.life -= dt * 3.0

            if particle.update(dt, damping=0.92):
                alive_particles.append(particle)

        self.stt_particles = alive_particles

    def update_audio_levels(self, levels: List[float]):
        self.audio_levels = levels[:20]
        self.style.update_audio_levels(self.audio_levels)

    @property
    def hands_free(self) -> bool:
        """Whether the current recording is latched on (kept while hidden too)."""
        return self._hands_free

    def set_hands_free(self, on: bool) -> None:
        """Mark a push-and-hold recording latched on until the next press.

        Kept even while hidden, for the Omarchy bar; cleared when the
        recording ends, the next one starts, or the overlay hides.
        """
        if bool(on) != self._hands_free:
            self._hands_free = bool(on)
            self.update()

    def set_language(self, code: str, choices) -> None:
        """Show the active dictation language when there is more than one."""
        code = code or ""
        if code != self._language and self._language and self.isVisible():
            self._language_changed_at = time.monotonic()
        self._language = code
        self._language_choices = tuple(choices or ())
        self.update()

    def show_language_notice(self) -> None:
        """Briefly show the active language near the pointer; a repeat replays it."""
        if self.current_state == self.STATE_LANGUAGE and self.isVisible():
            self.animation_time = 0.0
            self.hidden_timer.start(config.OVERLAY_HIDE_DELAY_MS)
            self.update()
            return
        self.show_at_cursor(self.STATE_LANGUAGE)

    def _refresh_language(self) -> None:
        try:
            from services import dictation_language

            settings = settings_manager.load_all_settings()
            choices = dictation_language.language_choices(settings)
            code = dictation_language.current_language(settings) if len(choices) > 1 else ""
        except Exception:
            logger.debug("Dictation language unavailable for the overlay", exc_info=True)
            choices, code = [], ""
        self._language_changed_at = None
        self.set_language(code, choices)

    def show_caption(self, text: str) -> None:
        """Show a short notice, such as a microphone switch, with the waveform.

        Ignored while hidden: the status line carries the same news.
        """
        if not text or not self.isVisible():
            return
        self._caption = text
        self._caption_started = time.monotonic()
        self._caption_timer.start(CAPTION_MS)
        self.update()

    def _clear_caption(self) -> None:
        self._caption_timer.stop()
        if self._caption:
            self._caption = ""
            self.update()

    def _end_listening_extras(self) -> None:
        self._hands_free = False
        self._language_changed_at = None
        self._chip_rect = QRectF()
        self._set_chip_hover(False)
        self._clear_caption()

    def hide(self):
        """Hide the overlay and stop animations."""
        self.timer.stop()
        self.hidden_timer.stop()

        self.current_state = self.STATE_IDLE
        self.animation_time = 0.0
        self.cancel_progress = 0.0
        self._streaming_preview_text = ""
        self._anchor_pos = None
        self._end_listening_extras()
        self._language = ""
        self._language_choices = ()
        if self.overlay_height != self._base_height:
            self.overlay_height = self._base_height
            self.setFixedSize(self.overlay_width, self.overlay_height)

        super().hide()

    def show_at_cursor(self, state: Optional[str] = None):
        """Show overlay near the cursor with optional state.

        Positions below-right of the cursor when possible, flipping and clamping
        so the overlay stays fully inside the monitor's available geometry.

        Args:
            state: Optional state to set. If None, uses current state or RECORDING as default.
        """
        if self._embedded and not self.parentWidget().isVisible():
            return
        self._anchor_pos = QCursor.pos()
        self._reposition_near_anchor()
        self.show()
        if self._embedded:
            self.raise_()

        if state is not None:
            self.set_state(state)
        elif self.current_state == self.STATE_IDLE:
            self.set_state(self.STATE_RECORDING)

        # Height may change when entering streaming; re-clamp after state apply.
        self._reposition_near_anchor()

    def closeEvent(self, event):
        self.timer.stop()
        self.hidden_timer.stop()
        self._caption_timer.stop()
        event.accept()

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent

        if self._embedded and obj is self.parentWidget():
            if event.type() == QEvent.Type.Resize:
                self._apply_streaming_height()
                self._reposition_near_anchor()
            elif event.type() == QEvent.Type.Hide:
                self.hide()
        return super().eventFilter(obj, event)
