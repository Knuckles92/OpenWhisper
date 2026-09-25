"""Live strip under the learned-rule composer, and the new-rule glow.

Teaching a rule by voice runs steps the user would otherwise wait through in
silence: the microphone is open, the dictation engine transcribes the clip,
and the cleanup model rewrites the words as a rule. The strip names the step
in flight and where it runs. Its motion follows real work only: the trace is
the microphone's own level, the sweep runs while a step is in flight, and the
glow marks a rule the moment it is saved.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Deque, Final, List

from PyQt6.QtCore import (
    QAbstractAnimation,
    QElapsedTimer,
    QEasingCurve,
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PyQt6.QtGui import QColor, QConicalGradient, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QWidget,
)

from ui_qt.utils.collapse_animation import (
    UNLIMITED_HEIGHT,
    create_max_height_animation,
    run_max_height_animation,
)
from ui_qt.utils.palette import token_color
from ui_qt.widgets.eliding_label import ElidingLabel

IDLE: Final[str] = "idle"
LISTENING: Final[str] = "listening"
TRANSCRIBING: Final[str] = "transcribing"
POLISHING: Final[str] = "polishing"

_TICK_MS: Final[int] = 16
#: One bar per step, about sixteen a second: slow enough to read as speech.
_STEP_MS: Final[float] = 60.0
#: Levels are RMS fractions of full scale. Speech on a desk microphone sits
#: around -40 to -15 dBFS, so this window gives it most of the bar height
#: while room noise stays on the dotted baseline.
_FLOOR_DB: Final[float] = -60.0
_CEILING_DB: Final[float] = -12.0
_BAR_WIDTH: Final[float] = 3.0
_BAR_PITCH: Final[float] = 5.0
_EASE: Final[float] = 0.35
_PULSE_MS: Final[int] = 1400
#: The engine status dot's orbit, so work in flight looks the same everywhere.
_ORBIT_MS: Final[float] = 1200.0
_SWEEP_MS: Final[int] = 1300
#: Work that ends this fast (a missing API key fails at once) never opens
#: the strip, so it does not flash open and shut behind the review dialog.
_WORK_REVEAL_DELAY_MS: Final[int] = 150
#: The clock turns amber this close to the automatic stop.
_CAP_WARNING_S: Final[int] = 10
_GLOW_MS: Final[int] = 1600


def level_to_height(level: float) -> float:
    """Map an RMS level (a 0-1 fraction of full scale) to a 0-1 bar height.

    The scale is logarithmic, as loudness is heard, so quiet speech still
    moves the trace and a shout does not flatten everything else.
    """
    if level <= 0.0:
        return 0.0
    db = 20.0 * math.log10(level)
    return max(0.0, min(1.0, (db - _FLOOR_DB) / (_CEILING_DB - _FLOOR_DB)))


def _format_clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _mix(start: QColor, end: QColor, t: float, alpha: float) -> QColor:
    t = min(1.0, max(0.0, t))
    color = QColor(
        round(start.red() + (end.red() - start.red()) * t),
        round(start.green() + (end.green() - start.green()) * t),
        round(start.blue() + (end.blue() - start.blue()) * t),
    )
    color.setAlphaF(min(1.0, max(0.0, alpha)))
    return color


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class _ActivityGlyph(QWidget):
    """A pulsing live dot while listening; the engine dot's orbit while working."""

    SIZE = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._state = IDLE
        self._clock = QElapsedTimer()
        self._clock.start()

    def set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._clock.restart()
        self.update()

    def paintEvent(self, _event) -> None:
        if self._state == IDLE:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        center = QRectF(self.rect()).center()
        elapsed = self._clock.elapsed()
        if self._state == LISTENING:
            phase = (elapsed % _PULSE_MS) / _PULSE_MS
            radius = 3.5 + 5.0 * (1.0 - (1.0 - phase) ** 3)
            painter.setBrush(token_color("danger", round(120 * (1.0 - phase))))
            painter.drawEllipse(center, radius, radius)
            painter.setBrush(token_color("danger"))
            painter.drawEllipse(center, 3.5, 3.5)
            return

        phase = elapsed / _ORBIT_MS
        ring = QRectF(self.rect()).adjusted(2.5, 2.5, -2.5, -2.5)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(token_color("accent", 40), 2))
        painter.drawEllipse(ring)
        tail = QConicalGradient(center, -phase * 360)
        tail.setColorAt(0, token_color("accent-cyan"))
        tail.setColorAt(0.18, token_color("accent"))
        tail.setColorAt(0.75, token_color("accent", 0))
        tail.setColorAt(1, token_color("accent", 0))
        painter.setPen(QPen(tail, 2))
        painter.drawEllipse(ring)


class LevelTrace(QWidget):
    """Scrolling bars of the microphone level; a sweep once capture ends.

    While listening, each step banks one bar holding the loudest level heard
    during it, and the bars slide left continuously between steps. When the
    microphone closes the bars hold still and a highlight passes over them
    while the clip is worked on; typed text gets a dotted rule instead.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(96)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._state = IDLE
        #: ``[target, shown]`` heights in 0-1, oldest first.
        self._bars: Deque[List[float]] = deque(maxlen=480)
        self._peak = 0.0
        self._steps = 0
        self._clock = QElapsedTimer()

    def sizeHint(self) -> QSize:
        return QSize(240, 22)

    def bar_heights(self) -> List[float]:
        """Target heights of the banked bars, oldest first."""
        return [bar[0] for bar in self._bars]

    def start_listening(self) -> None:
        self._bars.clear()
        self._peak = 0.0
        self._steps = 0
        self._state = LISTENING
        self._clock.start()
        self.update()

    def push_level(self, level: float) -> None:
        """Take one level from the recorder; the loudest in each step wins."""
        if self._state == LISTENING:
            self._peak = max(self._peak, float(level))

    def hold(self, state: str) -> None:
        """Stop scrolling and sweep the captured bars while ``state`` runs."""
        for bar in self._bars:
            bar[0] = bar[1]
        self._state = state
        self._clock.start()
        self.update()

    def clear(self) -> None:
        self._state = IDLE
        self._bars.clear()
        self.update()

    def advance(self) -> None:
        """One animation tick: bank the steps that ended and ease the bars."""
        if self._state == LISTENING and self._clock.isValid():
            due = int(self._clock.elapsed() // _STEP_MS)
            while self._steps < due:
                self._bars.append([level_to_height(self._peak), 0.0])
                self._peak = 0.0
                self._steps += 1
        for bar in self._bars:
            bar[1] += (bar[0] - bar[1]) * _EASE
        self.update()

    def paintEvent(self, _event) -> None:
        if self._state == IDLE:
            return
        rect = QRectF(self.rect())
        if rect.width() <= 0 or rect.height() <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        slots = int(rect.width() // _BAR_PITCH) + 1
        shown = [bar[1] for bar in reversed(self._bars)][:slots]
        # Silence before the first bar is the dotted baseline, so a new take
        # reads as a track filling up rather than a half-empty strip.
        shown += [0.0] * (slots - len(shown))
        listening = self._state == LISTENING
        elapsed = self._clock.elapsed() if self._clock.isValid() else 0
        sweep = band = 0.0
        if listening:
            # Slide left between steps so the trace moves continuously.
            offset = (elapsed % _STEP_MS) / _STEP_MS * _BAR_PITCH
        else:
            offset = 0.0
            band = max(rect.width() * 0.3, 48.0)
            phase = (elapsed % _SWEEP_MS) / _SWEEP_MS
            eased = phase * phase * (3.0 - 2.0 * phase)
            sweep = rect.left() - band + (rect.width() + 2.0 * band) * eased

        start = token_color("accent")
        end = token_color("accent-cyan")
        fade = rect.width() * 0.3
        middle = rect.center().y()
        tallest = rect.height() - 2.0
        for i, height in enumerate(shown):
            right = rect.right() - i * _BAR_PITCH - offset
            left = right - _BAR_WIDTH
            if right < rect.left():
                break
            # Older bars fade out toward the left edge.
            alpha = min(1.0, max(0.0, (left - rect.left()) / fade))
            if not listening:
                center = left + _BAR_WIDTH / 2.0
                glow = max(0.0, 1.0 - abs(center - sweep) / (band / 2.0))
                alpha *= 0.35 + 0.65 * glow
                height = max(height, 0.3 * glow)
            if alpha <= 0.01:
                continue
            bar = max(_BAR_WIDTH, height * tallest)
            painter.setBrush(
                _mix(start, end, (left - rect.left()) / rect.width(), alpha)
            )
            painter.drawRoundedRect(
                QRectF(left, middle - bar / 2.0, _BAR_WIDTH, bar),
                _BAR_WIDTH / 2.0,
                _BAR_WIDTH / 2.0,
            )


class RuleActivityStrip(QWidget):
    """Names the composer's step in flight and where that step runs.

    Hidden while idle. It opens under the input when dictation starts, and
    after a short delay when a typed rule starts polishing, and closes when
    the step ends. The panel keeps its full height and this host reveals it
    by clipping, so it slides open instead of being squashed.
    """

    #: Space above the panel. It lives inside the animated height, so the
    #: tile grows smoothly instead of jumping by a layout spacing.
    GAP = 8

    def __init__(self, cap_seconds: int, parent=None):
        """Build the strip, collapsed.

        Args:
            cap_seconds: Length of the dictation's automatic stop, shown
                beside the running clock.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setObjectName("ruleActivityHost")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._cap_seconds = max(1, int(cap_seconds))
        self._state = IDLE
        self._closing = False
        self._near_cap = False
        self._listen_clock = QElapsedTimer()

        self.panel = QFrame(self)
        self.panel.setObjectName("ruleActivityStrip")
        self.panel.setProperty("state", IDLE)
        row = QHBoxLayout(self.panel)
        row.setContentsMargins(12, 7, 12, 7)
        row.setSpacing(10)
        self.glyph = _ActivityGlyph(self.panel)
        row.addWidget(self.glyph, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.title_label = QLabel(self.panel)
        self.title_label.setObjectName("ruleActivityTitle")
        row.addWidget(self.title_label)
        # The detail keeps its text width and elides only when space runs
        # out; the trace takes everything else.
        self.detail_label = ElidingLabel(parent=self.panel)
        self.detail_label.setObjectName("ruleActivityDetail")
        row.addWidget(self.detail_label)
        self.trace = LevelTrace(self.panel)
        row.addWidget(self.trace, stretch=1)
        self.time_label = QLabel(self.panel)
        self.time_label.setObjectName("ruleActivityTime")
        self.time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.time_label)
        # The panel is placed by hand, so its size changes (a font scale
        # change, say) have to be passed on to this host's layout.
        self.panel.installEventFilter(self)

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.setInterval(_WORK_REVEAL_DELAY_MS)
        self._reveal_timer.timeout.connect(self._open)
        self._height_anim = create_max_height_animation(self, self)
        self.setMaximumHeight(0)
        self.hide()

    @property
    def state(self) -> str:
        return self._state

    def sizeHint(self) -> QSize:
        panel = self.panel.sizeHint()
        return QSize(panel.width(), self.GAP + panel.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(self.panel.minimumSizeHint().width(), 0)

    def show_listening(self, detail: str) -> None:
        """Open at once: the microphone is live from this moment.

        Args:
            detail: The input device being recorded.
        """
        self._reveal_timer.stop()
        self._enter(LISTENING, "Listening", detail)
        self.trace.start_listening()
        self._listen_clock.start()
        self._near_cap = False
        self._repolish_clock(False)
        self.time_label.show()
        self._update_clock()
        self._open()

    def show_transcribing(self, detail: str) -> None:
        """The dictation engine has the clip; ``detail`` says where it runs."""
        self._show_work(TRANSCRIBING, "Transcribing", detail)

    def show_polishing(self, detail: str) -> None:
        """The cleanup model is rewriting the words; ``detail`` names it."""
        self._show_work(POLISHING, "Polishing with AI", detail)

    def push_level(self, level: float) -> None:
        self.trace.push_level(level)

    def finish(self, animate: bool = True) -> None:
        """Close the strip; its last frame holds still while it slides shut."""
        self._reveal_timer.stop()
        if self.isHidden():
            self._reset()
            return
        self._closing = True
        self._timer.stop()
        if not animate:
            self._height_anim.stop()
            self._on_closed()
            return
        run_max_height_animation(
            self._height_anim,
            start=min(self.maximumHeight(), self.height()),
            end=0,
            on_finished=self._on_closed,
        )

    def _show_work(self, state: str, title: str, detail: str) -> None:
        self._enter(state, title, detail)
        self.trace.hold(state)
        self.time_label.hide()
        if self._closing:
            self._open()
        elif self.isHidden():
            self._reveal_timer.start()

    def _enter(self, state: str, title: str, detail: str) -> None:
        self._state = state
        self.title_label.setText(title)
        self.detail_label.setText(detail)
        self.glyph.set_state(state)
        if self.panel.property("state") != state:
            self.panel.setProperty("state", state)
            _repolish(self.panel)
        self.setAccessibleName(title)
        self.setAccessibleDescription(detail)
        self._sync_timer()

    def _open(self) -> None:
        if self._state == IDLE:
            return
        self._closing = False
        if self.isHidden():
            self.setMaximumHeight(0)
            self.show()
        self._place_panel()
        self._sync_timer()
        target = self.sizeHint().height()
        current = min(self.maximumHeight(), self.height())
        running = self._height_anim.state() == QAbstractAnimation.State.Running
        if current >= target and not running:
            self.setMaximumHeight(UNLIMITED_HEIGHT)
            return
        run_max_height_animation(
            self._height_anim, start=current, end=target, on_finished=self._on_opened
        )

    def _on_opened(self) -> None:
        # Unbounded once open, so a larger font can still grow the panel.
        self.setMaximumHeight(UNLIMITED_HEIGHT)

    def _on_closed(self) -> None:
        self.hide()
        self.setMaximumHeight(0)
        self._closing = False
        self._reset()

    def _reset(self) -> None:
        self._state = IDLE
        self.trace.clear()
        self.glyph.set_state(IDLE)
        if self.panel.property("state") != IDLE:
            self.panel.setProperty("state", IDLE)
            _repolish(self.panel)
        self._sync_timer()

    def _sync_timer(self) -> None:
        if self._state != IDLE and self.isVisible() and not self._closing:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _tick(self) -> None:
        self.trace.advance()
        self.glyph.update()
        if self._state == LISTENING:
            self._update_clock()

    def _update_clock(self) -> None:
        elapsed = 0
        if self._listen_clock.isValid():
            elapsed = min(
                int(self._listen_clock.elapsed() // 1000), self._cap_seconds
            )
        text = f"{_format_clock(elapsed)} / {_format_clock(self._cap_seconds)}"
        if self.time_label.text() != text:
            self.time_label.setText(text)
        near = self._cap_seconds - elapsed <= _CAP_WARNING_S
        if near != self._near_cap:
            self._near_cap = near
            self._repolish_clock(near)

    def _repolish_clock(self, near: bool) -> None:
        self.time_label.setProperty("nearCap", near)
        _repolish(self.time_label)

    def _place_panel(self) -> None:
        self.panel.setGeometry(
            0, self.GAP, self.width(), self.panel.sizeHint().height()
        )

    def eventFilter(self, obj, event) -> bool:
        if obj is self.panel and event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()
            self._place_panel()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_panel()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()


class ItemGlow(QWidget):
    """Washes one list row in the accent colour and lets it fade.

    Marks a rule the moment it lands in the library. The wash is painted on a
    transparent layer over the view's viewport, so the list's own item
    styling is left alone.
    """

    def __init__(self, view: QListWidget):
        super().__init__(view.viewport())
        self._view = view
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._index = QPersistentModelIndex()
        self._strength = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(_GLOW_MS)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        # Holds near full strength, then fades quickly.
        self._anim.setEasingCurve(QEasingCurve.Type.InQuad)
        self._anim.valueChanged.connect(self._on_value)
        self._anim.finished.connect(self.hide)
        self.hide()

    @property
    def strength(self) -> float:
        return self._strength

    def flash(self, item: QListWidgetItem) -> None:
        """Scroll ``item`` into view and wash it."""
        self._index = QPersistentModelIndex(self._view.indexFromItem(item))
        self._view.scrollToItem(item)
        self._anim.stop()
        self._strength = 1.0
        self._follow_viewport()
        self.show()
        self.raise_()
        self._anim.start()

    def _on_value(self, value) -> None:
        self._strength = float(value)
        self._follow_viewport()
        self.update()

    def _follow_viewport(self) -> None:
        rect = self._view.viewport().rect()
        if self.geometry() != rect:
            self.setGeometry(rect)

    def paintEvent(self, _event) -> None:
        if self._strength <= 0.0 or not self._index.isValid():
            return
        rect = QRectF(self._view.visualRect(QModelIndex(self._index)))
        if rect.isEmpty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Match the rule rows' 9px radius and 1px border in theme.qss.
        rect = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(token_color("accent", round(255 * self._strength)), 1.0))
        painter.setBrush(token_color("accent", round(70 * self._strength)))
        painter.drawRoundedRect(rect, 9.0, 9.0)
