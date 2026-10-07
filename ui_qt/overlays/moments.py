"""The overlay's one-shot moments: a finished command, a toggle, a copy.

Each plays the same choreography so they read as one family. A lead-in
tells the moment's story (the rewrite's sparkles gather, dots spiral in);
then a disc pops in the moment's tone with two rings rolling out and the
pill's edge flashing, the glyph draws itself cut out of the disc, confetti
bursts, and the label rises in under a glint. The contents fade out just
before the overlay hides.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from itertools import pairwise

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

from ui_qt.utils.palette import token_color
from ui_qt.waveform_styles import Particle, round_pen

RADIUS = 13.0
POP_S = 0.24
RING_S = 0.6
GLYPH_DELAY_S = 0.1
GLYPH_S = 0.26
LABEL_DELAY_S = 0.08
LABEL_S = 0.3
GLINT_DELAY_S = 0.25
GLINT_S = 0.8
FADE_S = 0.3

# The cleaning and rewriting sparkles: (x_frac, y_frac, base_size,
# twinkle_phase). Phases are staggered so they shimmer in sequence.
SPARKLES = (
    (0.50, 0.42, 11.0, 0.0),
    (0.37, 0.28, 6.0, 1.3),
    (0.64, 0.30, 7.5, 2.6),
    (0.41, 0.58, 5.0, 3.9),
    (0.61, 0.55, 6.5, 5.2),
)
#: Small sparkles left twinkling beside an AI edit's check: (dx, dy, size, phase).
_AFTERGLOW = ((23.0, -11.0, 4.5, 0.0), (-22.0, -8.0, 3.5, 2.1), (19.0, 11.0, 3.0, 4.2))

#: Glyph strokes around the disc's centre, drawn in order, and pen widths.
GLYPHS = {
    "check": (((-5.5, 0.3), (-1.8, 4.0), (5.8, -4.4)),),
    "cross": (((-4.2, -4.2), (4.2, 4.2)), ((4.2, -4.2), (-4.2, 4.2))),
    "bang": (((0.0, -5.2), (0.0, 1.4)), ((0.0, 4.9), (0.0, 4.95))),
    "clip": (
        ((-2.2, -5.6), (-5.0, -5.6), (-5.0, 6.4), (5.0, 6.4), (5.0, -5.6), (2.2, -5.6)),
        ((-2.6, -7.2), (2.6, -7.2), (2.6, -4.2), (-2.6, -4.2), (-2.6, -7.2)),
        ((-2.6, -0.6), (2.6, -0.6)),
        ((-2.6, 2.8), (1.4, 2.8)),
    ),
}
_GLYPH_WIDTH = {"check": 2.6, "cross": 2.6, "bang": 2.4, "clip": 1.5}


@dataclass(frozen=True)
class Moment:
    """How one moment plays.

    Attributes:
        tone: Palette role of the disc, the second ring and the edge flash.
        partner: Role of the first ring, the lead-in and half the confetti.
        glyph: Key into ``GLYPHS``.
        lead_in: "sparkles" (the rewrite's), "converge" (dots spiral in),
            "collapse" (what was on screen implodes) or "".
        pop_at: When the disc pops, in seconds, after the lead-in.
        hues: Confetti hue ranges for the tone's half and the partner's half.
        glint: Role of the glint that passes over the label.
        fall: Gravity on the confetti; a switch turning off lets it drop.
        afterglow: Leave the rewrite's sparkles twinkling beside the glyph.
    """

    tone: str
    partner: str
    glyph: str
    lead_in: str
    pop_at: float
    hues: tuple
    glint: str
    fall: float = 0.0
    afterglow: bool = False


MOMENTS = {
    "done": Moment("success", "purple", "check", "sparkles", 0.22,
                   ((130, 150), (272, 292)), "purple-text", afterglow=True),
    "enabled": Moment("success", "accent-cyan", "check", "converge", 0.3,
                      ((128, 150), (182, 198)), "success-text"),
    "disabled": Moment("danger", "warning", "cross", "", 0.0,
                       ((0, 10), (24, 38)), "danger-text", fall=260.0),
    "copied": Moment("accent-cyan", "accent", "clip", "converge", 0.3,
                     ((188, 200), (206, 218)), "accent-cyan"),
}


#: Canceled is the one moment with weight: the recording collapses into a
#: bright core, the disc slams in from oversize, the whole pill flashes red
#: and a burst of glowing bubbles fills it.
CANCELED = Moment("danger", "warning", "cross", "collapse", 0.16,
                  ((0, 12), (18, 40)), "danger-text")


def phase(now: float, start: float, length: float) -> float:
    """How far ``now`` is through the window ``[start, start + length]``, 0-1."""
    return max(0.0, min(1.0, (now - start) / length))


def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def ease_out_back(t: float, overshoot: float = 2.2) -> float:
    """Runs past 1 and settles back, so a shape pops rather than slides."""
    t -= 1.0
    return 1.0 + (overshoot + 1.0) * t ** 3 + overshoot * t ** 2


def exit_opacity(now: float, total: float) -> float:
    """1 until the last ``FADE_S`` of a moment lasting ``total`` seconds."""
    return phase(total - now, 0.0, FADE_S)


def draw_sparkle(painter: QPainter, cx: float, cy: float, size: float, color: QColor) -> None:
    painter.setPen(round_pen(color, 2))
    painter.drawLine(int(cx), int(cy - size), int(cx), int(cy + size))
    painter.drawLine(int(cx - size), int(cy), int(cx + size), int(cy))

    accent = QColor(color)
    accent.setAlpha(int(color.alpha() * 0.55))
    diag = size * 0.45
    painter.setPen(round_pen(accent, 1.5))
    painter.drawLine(int(cx - diag), int(cy - diag), int(cx + diag), int(cy + diag))
    painter.drawLine(int(cx - diag), int(cy + diag), int(cx + diag), int(cy - diag))


def draw_gathering_sparkles(painter: QPainter, rect: QRectF, center: QPointF,
                            now: float, length: float) -> None:
    """The rewriting sparkles flying into ``center`` and shrinking."""
    gather = phase(now, 0.0, length)
    if gather >= 1.0:
        return
    pull = gather * gather
    for x_frac, y_frac, size, _offset in SPARKLES:
        sx, sy = x_frac * rect.width(), y_frac * rect.height() - 4
        draw_sparkle(
            painter,
            sx + (center.x() - sx) * pull,
            sy + (center.y() - sy) * pull,
            size * (1.0 - 0.7 * pull),
            token_color("purple", int(255 * (1.0 - 0.5 * gather))),
        )


def draw_converging(painter: QPainter, center: QPointF, now: float, moment: Moment) -> None:
    """Dots in the moment's two colours spiralling into ``center``."""
    gather = phase(now, 0.0, moment.pop_at)
    if gather >= 1.0:
        return
    pull = gather * gather
    count = 18
    painter.setPen(Qt.PenStyle.NoPen)
    for i in range(count):
        reach = 46.0 + 9.0 * (i * 7 % 3)
        angle = i / count * math.tau + 1.8 * pull
        radius = reach * (1.0 - pull)
        tone = moment.tone if i % 2 else moment.partner
        painter.setBrush(token_color(tone, int(255 * min(1.0, now / 0.06))))
        size = 2.6 * (1.0 - 0.5 * pull)
        # Flattened to the pill's shape, as the burst is.
        painter.drawEllipse(
            QPointF(center.x() + radius * math.cos(angle), center.y() + 0.5 * radius * math.sin(angle)),
            size, size,
        )


def shake_offset(now: float, start: float, length: float, amplitude: float, wiggles: float) -> float:
    """A head-shake: a horizontal wobble that dies away, for a "no"."""
    shake = phase(now, start, length)
    if not 0.0 < shake < 1.0:
        return 0.0
    return amplitude * math.sin(shake * math.tau * wiggles) * (1.0 - shake)


def draw_collapsing(painter: QPainter, center: QPointF, now: float, length: float, snapshot) -> None:
    """What was on screen, ``(x, y, color, radius)`` dots, sucked into ``center``."""
    pull = phase(now, 0.0, length) ** 3
    if pull >= 1.0:
        return
    painter.setPen(Qt.PenStyle.NoPen)
    for x, y, color, size in snapshot:
        painter.setBrush(color)
        radius = max(0.4, size * (1.0 - 0.8 * pull))
        painter.drawEllipse(
            QPointF(x + (center.x() - x) * pull, y + (center.y() - y) * pull), radius, radius
        )


def draw_pill_flash(painter: QPainter, rect: QRectF, now: float, start: float, tone: str) -> None:
    """The whole pill washes in ``tone`` with a bright edge, then clears."""
    flash = phase(now, start, 0.45)
    if not 0.0 < flash < 1.0:
        return
    fade = (1.0 - flash) ** 2
    tint = QPainterPath()
    tint.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
    painter.fillPath(tint, token_color(tone, int(70 * fade)))
    edge = QPainterPath()
    edge.addRoundedRect(rect.adjusted(1, 1, -1, -1), 11.5, 11.5)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(token_color(tone, int(240 * fade)), 2.2))
    painter.drawPath(edge)


def draw_core_flash(painter: QPainter, center: QPointF, now: float, start: float) -> None:
    """A bright core where everything imploded."""
    core = phase(now, start, 0.18)
    if not 0.0 < core < 1.0:
        return
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(token_color("warning-text-strong", int(230 * (1.0 - core))))
    radius = 6.0 + 20.0 * ease_out_cubic(core)
    painter.drawEllipse(center, radius, radius)


def draw_slam_disc(painter: QPainter, center: QPointF, now: float, start: float, tone: str) -> None:
    """A disc that slams in from oversize, the reverse of a pop, then breathes."""
    slam = phase(now, start, 0.22)
    if slam <= 0.0:
        return
    radius = RADIUS * (1.7 - 0.7 * ease_out_back(slam, 3.0))
    settled = phase(now, start + 0.22, 0.4)
    breathe = 0.5 + 0.5 * math.sin((now - start) * 2.6)
    halo = radius + 3.0 + 2.5 * breathe * settled
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(token_color(tone, int(40 + 22 * breathe * settled)))
    painter.drawEllipse(center, halo, halo)
    painter.setBrush(token_color(tone, int(255 * min(1.0, slam * 4))))
    painter.drawEllipse(center, radius, radius)


def bubble_burst(moment: Moment, cx: float, cy: float, count: int = 72) -> list[Particle]:
    """Big glowing bubbles that fill the pill: the original Canceled's look."""
    particles = []
    for i in range(count):
        angle = random.uniform(0, math.tau)
        speed = random.uniform(190, 360)
        particle = Particle(
            cx, cy, math.cos(angle) * speed, math.sin(angle) * speed * 0.6,
            hue=random.uniform(*moment.hues[i % 2]),
        )
        particle.size = random.uniform(2.5, 5.0)
        particles.append(particle)
    return particles


def advance_bubbles(particles: list[Particle], dt: float) -> list[Particle]:
    """Bubbles jostle and drift out; they outlive confetti."""
    alive = []
    for particle in particles:
        particle.vx += random.uniform(-25, 25) * dt
        particle.vy += random.uniform(-25, 25) * dt
        particle.life -= dt * 0.35
        if particle.update(dt, damping=0.93):
            alive.append(particle)
    return alive


def draw_bubbles(painter: QPainter, particles: list[Particle]) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    for particle in particles:
        color = particle.get_fading_color()
        size = particle.size * (0.5 + 0.5 * particle.life)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(particle.x - size, particle.y - size, size * 2, size * 2))
        if particle.life > 0.4:
            glow = QColor(color)
            glow.setAlpha(int(color.alpha() * 0.45))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(glow, 1))
            painter.drawEllipse(QRectF(particle.x - size - 2, particle.y - size - 2, size * 2 + 4, size * 2 + 4))
            painter.setPen(Qt.PenStyle.NoPen)


def draw_rings(painter: QPainter, center: QPointF, now: float, start: float,
               tones: tuple, reach: float = 20.0, radius: float = RADIUS) -> None:
    """Rings rolling out from the disc, one per tone, a beat apart."""
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for index, tone in enumerate(tones):
        ring = phase(now, start + 0.09 * index, RING_S)
        if 0.0 < ring < 1.0:
            fade = (1.0 - ring) ** 2
            size = radius + reach * ease_out_cubic(ring)
            painter.setPen(QPen(token_color(tone, int(210 * fade)), 0.6 + 2.4 * fade))
            painter.drawEllipse(center, size, size)


def draw_edge_flash(painter: QPainter, rect: QRectF, now: float, start: float, tone: str) -> None:
    """The pill's edge lights up in ``tone`` and fades."""
    edge = phase(now, start, RING_S)
    if not 0.0 < edge < 1.0:
        return
    outline = QPainterPath()
    outline.addRoundedRect(rect.adjusted(1, 1, -1, -1), 11.5, 11.5)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(token_color(tone, int(170 * (1.0 - edge) ** 2)), 1.6))
    painter.drawPath(outline)


def draw_disc(painter: QPainter, center: QPointF, now: float, start: float, tone: str,
              radius: float = RADIUS) -> None:
    """A solid disc that pops in, then breathes inside a soft halo."""
    pop = phase(now, start, POP_S)
    if pop <= 0.0:
        return
    size = radius * ease_out_back(pop)
    settled = phase(now, start + POP_S, 0.4)
    breathe = 0.5 + 0.5 * math.sin((now - start) * 2.6)
    halo = size + 3.0 + 2.5 * breathe * settled
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(token_color(tone, int(28 + 22 * breathe * settled)))
    painter.drawEllipse(center, halo, halo)
    painter.setBrush(token_color(tone))
    painter.drawEllipse(center, size, size)


def glyph_path(glyph: str, center: QPointF, amount: float) -> QPainterPath:
    """The glyph's first ``amount`` (0-1) of length, as if being drawn."""
    strokes = [[center + QPointF(x, y) for x, y in stroke] for stroke in GLYPHS[glyph]]
    remaining = amount * sum(_length(a, b) for stroke in strokes for a, b in pairwise(stroke))
    path = QPainterPath()
    for stroke in strokes:
        if remaining <= 0.0:
            break
        path.moveTo(stroke[0])
        for start, end in pairwise(stroke):
            step = min(1.0, remaining / max(_length(start, end), 1e-6))
            path.lineTo(start + (end - start) * step)
            remaining -= _length(start, end)
            if remaining <= 0.0:
                break
    return path


def _length(a: QPointF, b: QPointF) -> float:
    return math.hypot(b.x() - a.x(), b.y() - a.y())


def draw_glyph(painter: QPainter, center: QPointF, now: float, start: float, glyph: str) -> None:
    """Draw the glyph stroke by stroke, cut out of the disc.

    It takes the pill's own colour: dark on the dark theme, white on the light.
    """
    amount = phase(now, start, GLYPH_S)
    if amount <= 0.0:
        return
    ink = token_color("overlay-bg")
    ink.setAlpha(255)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(round_pen(ink, _GLYPH_WIDTH[glyph]))
    painter.drawPath(glyph_path(glyph, center, ease_out_cubic(amount)))


def draw_afterglow(painter: QPainter, center: QPointF, now: float, start: float) -> None:
    glow = phase(now, start, 0.3)
    if glow <= 0.0:
        return
    for dx, dy, size, offset in _AFTERGLOW:
        twinkle = 0.5 + 0.5 * math.sin(now * 3.0 + offset)
        draw_sparkle(
            painter, center.x() + dx, center.y() + dy, size * (0.6 + 0.4 * twinkle),
            token_color("purple", int(glow * (70 + 150 * twinkle))),
        )


def draw_label(painter: QPainter, rect: QRectF, text: str, now: float, start: float,
               glint: str, align=Qt.AlignmentFlag.AlignCenter, slide: QPointF | None = None) -> None:
    """The label rises into place; one glint in ``glint`` passes over it."""
    rise = phase(now, start, LABEL_S)
    if rise <= 0.0:
        return
    font = QFont("Segoe UI", 10, QFont.Weight.Bold)
    metrics = QFontMetrics(font)
    text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width()))
    offset = QPointF(0.0, 6.0) if slide is None else slide
    rect = rect.translated(offset * (1.0 - ease_out_cubic(rise)))
    alpha = int(255 * rise)
    base = token_color("overlay-text", alpha)
    shimmer = phase(now, start + GLINT_DELAY_S, GLINT_S)
    if 0.0 < shimmer < 1.0:
        width = metrics.horizontalAdvance(text)
        left = rect.center().x() - width / 2 if align & Qt.AlignmentFlag.AlignHCenter else rect.left()
        band = 28.0
        x = left - band + (width + 2 * band) * ease_out_cubic(shimmer)
        gradient = QLinearGradient(x - band, 0, x + band, 0)
        gradient.setColorAt(0.0, base)
        gradient.setColorAt(0.5, token_color(glint, alpha))
        gradient.setColorAt(1.0, base)
        painter.setPen(QPen(QBrush(gradient), 1))
    else:
        painter.setPen(QPen(base))
    painter.setFont(font)
    painter.drawText(rect, int(align), text)


def burst(moment: Moment, cx: float, cy: float, count: int = 40) -> list[Particle]:
    """Confetti in the moment's two colours, flattened to the pill's shape."""
    particles = []
    for i in range(count):
        angle = i / count * math.tau + random.uniform(-0.3, 0.3)
        speed = random.uniform(170, 280)
        particle = Particle(
            cx + random.uniform(-3, 3), cy + random.uniform(-3, 3),
            math.cos(angle) * speed, math.sin(angle) * speed * 0.35,
            hue=random.uniform(*moment.hues[i % 2]),
        )
        particle.size = random.uniform(1.6, 3.0)
        particles.append(particle)
    return particles


def advance_confetti(particles: list[Particle], dt: float, fall: float) -> list[Particle]:
    """One tick: a quick burst that fades, not a drift."""
    alive = []
    for particle in particles:
        particle.vy += fall * dt
        particle.life -= dt * 0.6
        if particle.update(dt, damping=0.92):
            alive.append(particle)
    return alive


def draw_confetti(painter: QPainter, particles: list[Particle]) -> None:
    """Solid dots: glow rings on dots this small read as bubbles."""
    painter.setPen(Qt.PenStyle.NoPen)
    for particle in particles:
        painter.setBrush(particle.get_fading_color())
        size = particle.size * particle.life
        painter.drawEllipse(QRectF(particle.x - size, particle.y - size, size * 2, size * 2))
