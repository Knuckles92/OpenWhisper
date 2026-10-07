"""What every live recording look shares: the voice meter and the live label.

The microphone reports raw RMS, where speech is only about 0.01-0.15 of
full scale, so particles fed that barely move while someone talks. The live
looks (see recording_looks) hear loudness on a decibel scale through
``VoiceRibbon``, which keeps the last couple of seconds with a level meter's
ballistics, and name the recording with a label whose live dot, words and
clock are each optional. Their motion follows the microphone only: silence
settles them.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Final

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

from ui_qt.overlays import moments
from ui_qt.utils.loudness import level_to_height
from ui_qt.utils.palette import current_palette, token_color

#: How far the particles' launch line sits above the overlay's bottom, as the
#: legacy emitter did.
BASELINE_FROM_BOTTOM: Final[float] = 30.0
#: The loudest speech lifts the ribbon this many pixels.
RISE: Final[float] = 22.0
HISTORY_S: Final[float] = 2.2
#: About 45 samples a second: finer than a syllable.
_SAMPLES: Final[int] = 100
#: A level meter's ballistics: a rise shows at once; a fall decays with this
#: time constant, quick enough that syllables stay apart.
RELEASE_S: Final[float] = 0.06
EDGE: Final[float] = 14.0
#: How strongly loudness drives particle emission in the live look; the
#: legacy look uses the particle config's own value.
AUDIO_RESPONSE: Final[float] = 2.6
_PULSE_S: Final[float] = 1.4


class VoiceRibbon:
    """The last ``HISTORY_S`` seconds of loudness, newest on the right."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.loudness = 0.0
        #: The meter's reading now: the live edge of the ribbon.
        self.shown = 0.0
        self._peak = 0.0
        self._slot_peak = 0.0
        self._history: deque = deque([0.0] * _SAMPLES, maxlen=_SAMPLES)
        self._carry = 0.0

    def hear(self, level: float) -> None:
        """Take the microphone's RMS level (0-1 of full scale)."""
        self.loudness = level_to_height(level)
        # Several levels can arrive between frames; the loudest one counts.
        self._peak = max(self._peak, self.loudness)

    def advance(self, dt: float) -> None:
        target = max(self._peak, self.loudness)
        self._peak = 0.0
        if target >= self.shown:
            self.shown = target
        else:
            self.shown += (target - self.shown) * (1.0 - math.exp(-dt / RELEASE_S))
        self._slot_peak = max(self._slot_peak, self.shown)
        self._carry += dt
        step = HISTORY_S / _SAMPLES
        while self._carry >= step:
            self._carry -= step
            self._history.append(self._slot_peak)
            self._slot_peak = self.shown

    @property
    def history(self) -> list:
        """Heights 0-1, oldest first."""
        return list(self._history)

    def height_at(self, x: float, width: float) -> float:
        """The ribbon's 0-1 height at ``x`` across an overlay ``width`` wide."""
        span = max(1.0, width - 2 * EDGE)
        f = min(1.0, max(0.0, (x - EDGE) / span))
        pos = f * (_SAMPLES - 1)
        i = int(pos)
        j = min(i + 1, _SAMPLES - 1)
        return self._history[i] + (self._history[j] - self._history[i]) * (pos - i)

    def crest(self, x: float, width: float, baseline: float) -> float:
        """Where a particle leaves from at ``x``: the ribbon's top edge."""
        return baseline - RISE * self.height_at(x, width)

    def draw(self, painter: QPainter, width: float, baseline: float, hue: float, grow: float) -> None:
        """A soft glowing band under a bright edge, in the particles' hues."""
        if grow <= 0.0:
            return
        xs = [EDGE + (width - 2 * EDGE) * i / 80 for i in range(81)]
        top = [QPointF(x, baseline - (RISE * self.height_at(x, width) + 1.2) * grow) for x in xs]
        area = QPainterPath(QPointF(xs[0], baseline + 1))
        for point in top:
            area.lineTo(point)
        area.lineTo(QPointF(xs[-1], baseline + 1))
        area.closeSubpath()
        fill = QLinearGradient(EDGE, 0, width - EDGE, 0)
        edge = QLinearGradient(EDGE, 0, width - EDGE, 0)
        # Faded at both ends; across, the hue drifts like the particles'.
        for stop, fill_alpha, edge_alpha in ((0.0, 0, 0), (0.12, 40, 150), (0.88, 60, 230), (1.0, 0, 0)):
            fill.setColorAt(stop, _ribbon_color(hue + stop * 60, int(fill_alpha * grow)))
            edge.setColorAt(stop, _ribbon_color(hue + stop * 60, int(edge_alpha * grow)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillPath(area, fill)
        line = QPainterPath(top[0])
        for point in top[1:]:
            line.lineTo(point)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(edge, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(line)


def _ribbon_color(hue: float, alpha: int) -> QColor:
    if current_palette().is_dark:
        color = QColor.fromHsv(int(hue) % 360, 190, 245)
    else:
        color = QColor.fromHsv(int(hue) % 360, 235, 205)
    color.setAlpha(max(0, min(255, alpha)))
    return color


def format_clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def draw_live_label(painter: QPainter, rect: QRectF, label: str, elapsed: float,
                    dot_tone: str, text_tone: str = "overlay-text", *,
                    dot: bool = True, text: bool = True, clock: bool = True) -> None:
    """``● Recording 0:04``, each part optional and the rest kept centred.

    The dot pops in with a ring, then pulses; the words rise into place.
    """
    font = QFont("Segoe UI", 10, QFont.Weight.Bold)
    clock_font = QFont("Segoe UI", 10)
    metrics, clock_metrics = QFontMetrics(font), QFontMetrics(clock_font)
    dot_d, gap = 8.0, 7.0
    clock_text = format_clock(elapsed) if clock else ""
    clock_w = clock_metrics.horizontalAdvance(clock_text) if clock else 0.0
    # (kind, width) for each part shown, with the gaps between them.
    parts = []
    if dot:
        parts.append(("dot", dot_d))
    if text:
        room = rect.width() - (dot_d + gap if dot else 0) - (clock_metrics.horizontalAdvance("00:00") + gap if clock else 0)
        label = metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(max(0, room)))
        parts.append(("text", metrics.horizontalAdvance(label)))
    if clock:
        parts.append(("clock", clock_w))
    if not parts:
        return
    width = sum(w for _, w in parts) + gap * (len(parts) - 1)
    x = rect.center().x() - width / 2
    rise = moments.ease_out_cubic(moments.phase(elapsed, 0.06, moments.LABEL_S))
    top = rect.top() + 6.0 * (1.0 - rise)
    flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    for kind, part_w in parts:
        if kind == "dot":
            _draw_live_dot(painter, QPointF(x + dot_d / 2, rect.center().y() + 1), elapsed, dot_tone, dot_d)
        elif kind == "text":
            painter.setFont(font)
            painter.setPen(QPen(token_color(text_tone, int(255 * rise))))
            painter.drawText(QRectF(x, top, part_w + 2, rect.height()), flags, label)
        else:
            painter.setFont(clock_font)
            # On its own the clock reads at full strength; beside the words it recedes.
            painter.setPen(QPen(token_color(text_tone, int((150 if text else 230) * rise))))
            painter.drawText(QRectF(x, top, part_w + 2, rect.height()), flags, clock_text)
        x += part_w + gap


def _draw_live_dot(painter: QPainter, center: QPointF, elapsed: float, tone: str, diameter: float) -> None:
    moments.draw_rings(painter, center, elapsed, 0.0, (tone,), reach=10.0, radius=diameter / 2)
    pop = moments.phase(elapsed, 0.0, moments.POP_S)
    if pop <= 0.0:
        return
    settle = moments.POP_S + 0.4
    painter.setPen(Qt.PenStyle.NoPen)
    if elapsed > settle:
        pulse = ((elapsed - settle) % _PULSE_S) / _PULSE_S
        radius = diameter / 2 + 6.0 * (1.0 - (1.0 - pulse) ** 3)
        painter.setBrush(token_color(tone, int(110 * (1.0 - pulse))))
        painter.drawEllipse(center, radius, radius)
    painter.setBrush(token_color(tone))
    radius = diameter / 2 * moments.ease_out_back(pop)
    painter.drawEllipse(center, radius, radius)


def particle_hue(style) -> float:
    """The hue the particles are emitted around right now."""
    return (style.animation_time * style.color_shift_speed) % 360

