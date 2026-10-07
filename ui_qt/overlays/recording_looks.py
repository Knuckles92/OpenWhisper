"""How the overlay draws a voice while recording, and how each look finishes.

Every look but Classic hears loudness on a decibel scale through the
overlay's ``VoiceRibbon`` (instant attack, quick release, peak hold), so it
moves with speech and settles in silence. Each look is a small class the
overlay plugs in: it may tune the particles, advance its own motion each
tick and draw the voice under the label.

When the recording ends each look also draws its own Processing and
Transcribing. It takes a snapshot of the last couple of seconds of voice
and morphs from it into a working animation over ``MORPH_S``; the
overlay runs that animation on an eased clock, so the step from Processing
to Transcribing speeds it up instead of jumping. Classic (``LEGACY``) is
the original particles fed raw RMS, with the shared particle vortex, and
has no class here.

The ocean looks (a school of fish under stacked swells) and Fizz live in
``ocean_looks``.
"""
from __future__ import annotations

import math
from typing import Callable, Final, Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QConicalGradient,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

from ui_qt.overlays import live_recording, moments
from ui_qt.utils.palette import current_palette

RIBBON: Final[str] = "ribbon"
BARS: Final[str] = "bars"
WAVE: Final[str] = "wave"
AURORA: Final[str] = "aurora"
PULSE: Final[str] = "pulse"
DOTS: Final[str] = "dots"
BAIT_BALL: Final[str] = "baitball"
FISH: Final[str] = "fish"
GLOW: Final[str] = "glow"
FIZZ: Final[str] = "fizz"
LEGACY: Final[str] = "legacy"

LOOKS: Final[tuple] = (RIBBON, BARS, WAVE, AURORA, PULSE, DOTS, BAIT_BALL, FISH, GLOW, FIZZ, LEGACY)
DEFAULT: Final[str] = RIBBON
#: What a stored value from an earlier build means now.
_ALIASES: Final[dict] = {"live": RIBBON, "orb": PULSE, "bubbles": BAIT_BALL}

EDGE: Final[float] = live_recording.EDGE
#: The voice is drawn around this height, above the label row.
CENTER_Y: Final[float] = 29.0
#: How long a look takes to morph from the recording into its work.
MORPH_S: Final[float] = 0.5


def normalize_look(value) -> str:
    """A stored look, its current name, or the default for anything unknown."""
    value = _ALIASES.get(value, value)
    return value if value in LOOKS else DEFAULT


def smooth(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def hue_color(hue: float, alpha: float = 255, *, bright: bool = True) -> QColor:
    """The particles' colour family at ``hue``, tuned for the current theme."""
    if current_palette().is_dark:
        color = QColor.fromHsv(int(hue) % 360, 190 if bright else 150, 245)
    else:
        color = QColor.fromHsv(int(hue) % 360, 235, 205)
    color.setAlpha(max(0, min(255, int(alpha))))
    return color


def lerp_point(a: QPointF, b: QPointF, t: float) -> QPointF:
    return a + (b - a) * t


class Visual:
    """One look. ``overlay`` is the WaveformOverlay drawing it."""

    #: Whether the recording particles are drawn with this look.
    particles: bool = True
    #: Particles emitted a second at rest, or None for the style's own rate.
    emission_rate: Optional[float] = None

    def __init__(self, overlay) -> None:
        self.overlay = overlay
        self.snapshot: list = [0.0] * 2
        self.reset()

    def reset(self) -> None:
        """A new recording starts."""

    def emitter(self) -> Optional[Callable[[float], float]]:
        """Where particles launch at x, or None for the flat line."""
        return None

    def advance(self, dt: float) -> None:
        """One recording tick, after the particles and the ribbon moved."""

    def draw(self, painter: QPainter, grow: float) -> None:
        raise NotImplementedError

    # -- the finish ---------------------------------------------------------------
    def begin_work(self) -> None:
        """The recording ended: keep what the voice last looked like."""
        self.snapshot = self.ribbon.history

    def advance_work(self, dt: float, rate: float) -> None:
        """One Processing/Transcribing tick; ``rate`` eases up for Transcribing."""

    def draw_work(self, painter: QPainter, morph: float, clock: float) -> None:
        """Processing and Transcribing.

        Args:
            morph: 0 at the end of the recording, 1 once fully working.
            clock: Seconds of work, run faster while transcribing.
        """
        raise NotImplementedError

    # -- shared helpers -----------------------------------------------------------
    @property
    def ribbon(self) -> live_recording.VoiceRibbon:
        return self.overlay._ribbon

    @property
    def width(self) -> float:
        return float(self.overlay.width())

    def hue(self) -> float:
        return live_recording.particle_hue(self.overlay.style)

    def snap_at(self, f: float) -> float:
        """The frozen voice at 0-1 across."""
        values = self.snapshot
        pos = max(0.0, min(1.0, f)) * (len(values) - 1)
        i = int(pos)
        j = min(i + 1, len(values) - 1)
        return values[i] + (values[j] - values[i]) * (pos - i)


class Ribbon(Visual):
    """A glowing band tracing the last couple of seconds; particles leave its crest.

    Finish: the ribbon rolls itself up into a spinning loop.
    """

    def _baseline(self) -> float:
        return self.overlay._base_height - live_recording.BASELINE_FROM_BOTTOM

    def emitter(self):
        return lambda x: self.ribbon.crest(x, self.overlay.overlay_width, self._baseline())

    def draw(self, painter, grow):
        self.ribbon.draw(painter, self.width, self._baseline(), self.hue(), grow)

    def draw_work(self, painter, morph, clock):
        w = self.width
        base = self._baseline()
        spin = clock * 1.6 * math.pi
        cx, cy = w / 2, CENTER_Y - 2
        hue = self.hue()
        n = 80
        points = []
        for i in range(n + 1):
            f = i / n
            h = self.snap_at(f)
            line = QPointF(EDGE + (w - 2 * EDGE) * f, base - live_recording.RISE * h)
            angle = f * math.tau + spin
            r = 13 + 6 * h * (1 - 0.5 * morph)
            ring = QPointF(cx + r * math.cos(angle) * 1.25, cy + r * math.sin(angle))
            points.append(lerp_point(line, ring, morph))
        for i in range(n):
            tail = i / n
            painter.setPen(QPen(hue_color(hue + tail * 90, 40 + 215 * tail), 1.4 + 1.6 * tail,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(points[i], points[i + 1])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(hue_color(hue + 90, 255, bright=False))
        painter.drawEllipse(points[-1], 2.6 * morph + 0.5, 2.6 * morph + 0.5)


class Bars(Visual):
    """A scrolling meter of rounded bars, mirrored about the middle.

    Finish: the bars fly into a ring of spokes with a highlight running round.
    """

    particles = False
    PITCH = 5.0
    BAR = 3.0
    HALF = 18.0
    SPOKES = 28

    def draw(self, painter, grow):
        history = self.ribbon.history
        span = self.width - 2 * EDGE
        slots = max(1, int(span // self.PITCH))
        hue = self.hue()
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(slots):
            # Newest at the right; each bar is one history sample.
            index = len(history) - 1 - (slots - 1 - i) * len(history) // slots
            level = history[max(0, index)]
            x = EDGE + i * self.PITCH
            fade = min(1.0, (x - EDGE) / (span * 0.25))
            half = (1.5 + self.HALF * level) * grow
            painter.setBrush(hue_color(hue + i / slots * 60, (120 + 135 * level) * fade))
            painter.drawRoundedRect(QRectF(x, CENTER_Y - half, self.BAR, half * 2), self.BAR / 2, self.BAR / 2)

    def draw_work(self, painter, morph, clock):
        w = self.width
        cx, cy = w / 2, CENTER_Y - 1
        head = clock % 1.0
        hue = self.hue()
        for k in range(self.SPOKES):
            f = k / self.SPOKES
            h = self.snap_at(f)
            x0 = EDGE + (w - 2 * EDGE) * f
            angle = f * math.tau - math.pi / 2
            glow = max(0.0, 1.0 - ((f - head) % 1.0) * 3.2)
            inner, length = 9.0, 4 + 9 * (0.35 * h + 0.65 * glow)
            a = lerp_point(QPointF(x0, CENTER_Y - (1.5 + self.HALF * h)),
                           QPointF(cx + inner * math.cos(angle), cy + inner * math.sin(angle)), morph)
            b = lerp_point(QPointF(x0, CENTER_Y + (1.5 + self.HALF * h)),
                           QPointF(cx + (inner + length) * math.cos(angle), cy + (inner + length) * math.sin(angle)),
                           morph)
            alpha = 80 + 175 * max(glow * morph, (1 - morph) * (0.4 + h))
            painter.setPen(QPen(hue_color(hue + f * 60, alpha), self.BAR, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(a, b)


class MirrorWave(Visual):
    """The voice's outline as a glowing shape, mirrored; particles rise off its top.

    Finish: the outline gathers into a short glowing capsule that pulses.
    """

    AMPLITUDE = 18.0

    def emitter(self):
        return lambda x: CENTER_Y - self.AMPLITUDE * self.ribbon.height_at(x, self.overlay.overlay_width)

    def draw(self, painter, grow):
        w = self.width
        xs = [EDGE + (w - 2 * EDGE) * i / 90 for i in range(91)]
        amplitude = [(1.0 + self.AMPLITUDE * self.ribbon.height_at(x, w)) * grow for x in xs]
        self._draw_shape(painter, xs, amplitude, CENTER_Y, grow)

    def _draw_shape(self, painter, xs, amplitude, cy, grow):
        shape = QPainterPath(QPointF(xs[0], cy - amplitude[0]))
        for x, a in zip(xs[1:], amplitude[1:], strict=True):
            shape.lineTo(QPointF(x, cy - a))
        for x, a in zip(reversed(xs), reversed(amplitude), strict=True):
            shape.lineTo(QPointF(x, cy + a))
        shape.closeSubpath()
        hue = self.hue()
        left, right = xs[0], xs[-1]
        fill = QLinearGradient(left, 0, right, 0)
        edge = QLinearGradient(left, 0, right, 0)
        for stop, fill_alpha, edge_alpha in ((0.0, 0, 0), (0.15, 50, 140), (0.9, 90, 240), (1.0, 40, 200)):
            fill.setColorAt(stop, hue_color(hue + stop * 60, fill_alpha * grow))
            edge.setColorAt(stop, hue_color(hue + stop * 60, edge_alpha * grow))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillPath(shape, fill)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(edge, 1.4))
        painter.drawPath(shape)

    def draw_work(self, painter, morph, clock):
        w = self.width
        pulse = 0.5 + 0.5 * math.sin(clock * 4.0)
        hue = self.hue()
        half_span = (w - 2 * EDGE) / 2
        half_w = half_span + (26 + 8 * pulse - half_span) * morph
        top, bottom = [], []
        for i in range(61):
            f = i / 60
            x = w / 2 - half_w + 2 * half_w * f
            frozen = 1.0 + self.AMPLITUDE * self.snap_at(f)
            capsule = (6 + 2 * pulse) * math.sin(math.pi * f) ** 0.35
            a = frozen + (capsule - frozen) * morph
            top.append(QPointF(x, CENTER_Y - a))
            bottom.append(QPointF(x, CENTER_Y + a))
        shape = QPainterPath(top[0])
        for point in top[1:]:
            shape.lineTo(point)
        for point in reversed(bottom):
            shape.lineTo(point)
        shape.closeSubpath()
        fill = QLinearGradient(w / 2 - half_w, 0, w / 2 + half_w, 0)
        fill.setColorAt(0.0, hue_color(hue, 120))
        fill.setColorAt(0.5, hue_color(hue + 50, 230))
        fill.setColorAt(1.0, hue_color(hue + 100, 120))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(hue_color(hue + 50, 50 * pulse * morph))
        painter.drawRoundedRect(QRectF(w / 2 - half_w - 6, CENTER_Y - 14, 2 * half_w + 12, 28), 14, 14)
        painter.setBrush(fill)
        painter.drawPath(shape)


class Aurora(Visual):
    """Three flowing waves whose height and pace follow the voice right now.

    Finish: the three waves twist into one braid that spins.
    """

    particles = False
    WAVES = ((2.2, 0.0, 2.2), (3.1, 1.9, 1.6), (1.6, 3.7, 1.2))

    def reset(self):
        self._phase = 0.0
        self._amplitude = 0.0

    def advance(self, dt):
        level = self.ribbon.shown
        self._amplitude += (level - self._amplitude) * (1 - math.exp(-dt / 0.05))
        self._phase += dt * (2.0 + 7.0 * level)

    def advance_work(self, dt, rate):
        self._phase += dt * 6.0 * rate

    def _draw_waves(self, painter, specs, hue_drift=0.0):
        span = self.width - 2 * EDGE
        hue = self.hue()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for k, (freq, phase, amplitude, pen) in enumerate(specs):
            path = QPainterPath()
            for i in range(101):
                envelope = math.sin(math.pi * i / 100) ** 1.4
                y = CENTER_Y + amplitude * envelope * math.sin(freq * math.tau * i / 100 + phase)
                point = QPointF(EDGE + span * i / 100, y)
                if i:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
            color = hue + k * 45 + hue_drift
            painter.setPen(QPen(hue_color(color, 60), pen + 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawPath(path)
            painter.setPen(QPen(hue_color(color, 230), pen, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawPath(path)

    def draw(self, painter, grow):
        specs = [(freq, self._phase * (1 + 0.3 * k) + shift, (1.5 + 19.0 * self._amplitude * (1.0 - 0.22 * k)) * grow, pen)
                 for k, (freq, shift, pen) in enumerate(self.WAVES)]
        self._draw_waves(painter, specs)

    def draw_work(self, painter, morph, clock):
        specs = []
        for k, (freq, shift, pen) in enumerate(self.WAVES):
            braid_freq = freq + (2.0 - freq) * morph
            braid_shift = shift + (k * math.tau / 3 - shift) * morph
            amplitude = (1.5 + 19 * self._amplitude * (1 - 0.22 * k)) * (1 - morph) + 9.0 * morph
            specs.append((braid_freq, self._phase + braid_shift, amplitude, pen))
        self._draw_waves(painter, specs, clock * 40)


class Pulse(Visual):
    """A clean orb: a solid core and one crisp ring that follow the voice, with round ripples.

    Finish: the ring shrinks into a comet arc that spins round the core.
    """

    particles = False
    RIPPLE_S = 0.8

    def reset(self):
        self._ripples: list = []
        self._last = 0.0
        self._last_ripple = -1.0
        self._time = 0.0

    def advance(self, dt):
        self._time += dt
        level = self.ribbon.shown
        # A word starting: the level jumps up past a threshold.
        if level > 0.5 and level - self._last > 0.12 and self._time - self._last_ripple > 0.16:
            self._ripples.append(self._time)
            self._last_ripple = self._time
        self._last = level
        self._ripples = [r for r in self._ripples if self._time - r < self.RIPPLE_S]

    def advance_work(self, dt, rate):
        self._time += dt
        self._ripples = [r for r in self._ripples if self._time - r < self.RIPPLE_S]

    def _center(self) -> QPointF:
        return QPointF(self.width / 2, CENTER_Y - 1)

    def _draw_ripples(self, painter, ring, hue):
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for start in self._ripples:
            age = (self._time - start) / self.RIPPLE_S
            r = ring + 22 * moments.ease_out_cubic(age)
            painter.setPen(QPen(hue_color(hue, 200 * (1 - age) ** 2), 1.6 * (1 - age) + 0.5))
            painter.drawEllipse(self._center(), r, r)

    def draw(self, painter, grow):
        center = self._center()
        level = self.ribbon.shown
        hue = self.hue()
        pop = moments.ease_out_back(grow) if grow < 1 else 1.0
        ring = (11.0 + 8.0 * level) * pop
        core = (6.0 + 3.0 * level) * pop
        self._draw_ripples(painter, ring, hue)
        painter.setPen(QPen(hue_color(hue + 30, 230 * grow), 2.0))
        painter.setBrush(hue_color(hue, 60 * grow))
        painter.drawEllipse(center, ring, ring)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(hue_color(hue + 60, 255, bright=False))
        painter.drawEllipse(center, core, core)

    def draw_work(self, painter, morph, clock):
        center = self._center()
        hue = self.hue()
        level = self.snap_at(1.0)
        ring = (11.0 + 8.0 * level) + (14.0 - (11.0 + 8.0 * level)) * morph
        self._draw_ripples(painter, ring, hue)
        # The full ring narrows to a comet that spins: the lit share of the
        # gradient shrinks from all of it to a quarter.
        lit = 1.0 - 0.72 * morph
        spin = clock * 430
        sweep = QConicalGradient(center, -spin)
        sweep.setColorAt(0.0, hue_color(hue + 60, 255, bright=False))
        sweep.setColorAt(max(0.01, lit * 0.35), hue_color(hue + 30, 220))
        sweep.setColorAt(max(0.02, lit), hue_color(hue, 230 * (1 - morph)))
        sweep.setColorAt(1.0, hue_color(hue, 230 * (1 - morph)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(hue_color(hue, 50 * morph), 2.2))
        painter.drawEllipse(center, ring, ring)
        painter.setPen(QPen(sweep, 2.0 + 0.6 * morph, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawEllipse(center, ring, ring)
        core = (6.0 + 3.0 * level) * (1 - morph) + (4.5 + 0.8 * math.sin(clock * 5)) * morph
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(hue_color(hue + 60, 255, bright=False))
        painter.drawEllipse(center, core, core)


class Dots(Visual):
    """Five dots that ripple with the voice, newest on the right.

    Finish: they drop into a row and bounce in turn like a typing indicator.
    """

    particles = False
    COUNT = 5
    SPACING = 20.0
    LAG = 5

    def _x(self, k: int) -> float:
        return self.width / 2 + (k - (self.COUNT - 1) / 2) * self.SPACING

    def _level(self, values, k):
        return values[max(0, len(values) - 1 - (self.COUNT - 1 - k) * self.LAG)]

    def draw(self, painter, grow):
        history = self.ribbon.history
        hue = self.hue()
        painter.setPen(Qt.PenStyle.NoPen)
        for k in range(self.COUNT):
            # Dots pop in one after another as the recording starts.
            appear = moments.ease_out_back(smooth(grow * 1.6 - k * 0.15)) if grow < 1 else 1.0
            level = self._level(history, k)
            center = QPointF(self._x(k), CENTER_Y - 9 * level)
            r = (3.5 + 3.5 * level) * max(0.0, appear)
            painter.setBrush(hue_color(hue + k * 18, 70 * level))
            painter.drawEllipse(center, r + 4, r + 4)
            painter.setBrush(hue_color(hue + k * 18, 255))
            painter.drawEllipse(center, r, r)

    def draw_work(self, painter, morph, clock):
        hue = self.hue()
        painter.setPen(Qt.PenStyle.NoPen)
        for k in range(self.COUNT):
            frozen = self._level(self.snapshot, k)
            bounce = max(0.0, math.sin(clock * 6.0 - k * 0.7))
            y = (CENTER_Y - 9 * frozen) + ((CENTER_Y + 2 - 9 * bounce) - (CENTER_Y - 9 * frozen)) * morph
            r = 3.5 + 1.8 * bounce * morph + 3.5 * frozen * (1 - morph)
            painter.setBrush(hue_color(hue + k * 18, 70 * bounce * morph))
            painter.drawEllipse(QPointF(self._x(k), y), r + 3, r + 3)
            painter.setBrush(hue_color(hue + k * 18, 255))
            painter.drawEllipse(QPointF(self._x(k), y), r, r)


def make_visual(look: str, overlay) -> Optional[Visual]:
    """The visual for ``look``, or None for Classic."""
    from ui_qt.overlays import ocean_looks

    visuals = {
        RIBBON: Ribbon, BARS: Bars, WAVE: MirrorWave, AURORA: Aurora, PULSE: Pulse, DOTS: Dots,
        BAIT_BALL: ocean_looks.BaitBall, FISH: ocean_looks.RealFish, GLOW: ocean_looks.DeepGlow,
        FIZZ: ocean_looks.Fizz,
    }
    visual = visuals.get(normalize_look(look))
    return visual(overlay) if visual else None
