"""The ocean looks and Fizz.

Ocean: a school of Classic's balls swims under horizontal layers stacked like
swells. The fish flock (keep apart, match heading, hold together) and the
voice sets how high the school swims and how fast. Each layer is a row of
springs with its own ever-rolling swell; the school pushes the bottom layer
up where it crowds, and each layer pushes the one above once they get too
close, so a surge heaves the whole stack. The school rises out of the deep
as a recording starts.

- Bait ball mills in a tight spinning ball under swell lines; it finishes by
  pulling into the centre and spinning faster under a dome of swell.
- Real fish swim as little teardrops under glowing bands; they finish by
  wheeling into a slow circle while light shimmers across the water.
- Deep-sea glow is bioluminescent fish with light trails in darker water;
  they finish by spiralling into a glowing whirl that sends out sonar pings.

Fizz: glasses that fill as bubbles race up and reach the surface; it
finishes with the levels rolling from glass to glass like a filling wave.
"""
from __future__ import annotations

import math
import random

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainterPath, QPen

from ui_qt.overlays.recording_looks import EDGE, MORPH_S, Visual, hue_color, smooth
from ui_qt.utils.palette import current_palette


class _School(Visual):
    """A flocking school of balls pushing up stacked swell layers."""

    particles = False
    FISH = 56
    BASE = 50.0
    TOP = 6.0
    LAYERS = 5
    POINTS = 48
    BOTTOM = 40.0     # the lowest layer's rest height
    GAP = 5.5         # rest spacing between layers
    GAP_MIN = 2.6     # closer than this and a layer shoves the one above
    SWELL = 1.8
    RANGE = 26.0
    COHESION = 1.4
    ALIGN = 1.6
    SEPARATE = 260.0
    TRAIL = 0
    #: The voice as the school feels it: a rise lands at once and a fall
    #: lingers through the gaps between words, so a phrase reads as one surge
    #: instead of syllables the flock's inertia averages away.
    ATTACK_S = 0.04
    RELEASE_S = 0.35
    #: Room noise reads about 0.15 on the meter; below this the school rests,
    #: so speech stands out against a calm sea rather than a restless one.
    VOICE_FLOOR = 0.15
    #: How hard a fish swims for the height the voice asks of it.
    STEER = 15.0

    def reset(self):
        self.rng = random.Random(5)
        self.fish = []
        width = max(200.0, self.width)
        for _ in range(self.FISH):
            # The school rises out of the deep as the recording starts: spread
            # across the water, low down, swimming up into place.
            self.fish.append({
                "x": self.rng.uniform(EDGE, width - EDGE), "y": self.BASE - self.rng.uniform(0.5, 3.0),
                "vx": self.rng.uniform(-25, 25), "vy": -self.rng.uniform(20, 60),
                "r": self.rng.uniform(1.4, 3.8), "hue": self.rng.uniform(0, 70),
                "phase": self.rng.uniform(0, math.tau), "trail": [],
            })
        self.u = [[0.0] * self.POINTS for _ in range(self.LAYERS)]
        self.uv = [[0.0] * self.POINTS for _ in range(self.LAYERS)]
        self._t = 0.0
        self.level = 0.0
        self.working = False
        self._work_t = 0.0
        self.rate = 1.0

    # -- the school ---------------------------------------------------------------
    def steer(self, f, lift, speed):
        """Where a fish heads while recording: an altitude riding a wave, and a current."""
        wave = 0.55 + 0.45 * math.sin(f["x"] * 0.035 - self._t * 2.2 + f["phase"] * 0.3)
        target = self.BASE - 3 - lift * wave
        return math.sin(self._t * 0.7 + f["y"] * 0.05) * speed * 0.8, (target - f["y"]) * self.STEER

    def steer_work(self, f, morph):
        """Where a fish heads while the recording is worked on."""
        return self.steer(f, 10.0, 30.0)

    def morph(self) -> float:
        return smooth(self._work_t / MORPH_S)

    def voice(self) -> float:
        """How strongly the voice shows, easing out as the work morphs in."""
        return self.level * (1.0 - self.morph()) if self.working else self.level

    def advance(self, dt):
        self._swim(dt)

    def advance_work(self, dt, rate):
        self.working = True
        self._work_t += dt
        self.rate = rate
        self._swim(dt)

    def begin_work(self):
        super().begin_work()
        self.working, self._work_t = True, 0.0

    def _swim(self, dt):
        if dt <= 0:
            return
        self._t += dt
        if not self.working:
            shown = max(0.0, self.ribbon.shown - self.VOICE_FLOOR) / (1.0 - self.VOICE_FLOOR)
            tau = self.ATTACK_S if shown > self.level else self.RELEASE_S
            self.level += (shown - self.level) * (1 - math.exp(-dt / tau))
        lift = 4 + 40 * self.level
        speed = 18 + 70 * self.level
        w = self.width
        morph = self.morph() if self.working else 0.0
        for f in self.fish:
            ax = ay = cx = cy = avx = avy = 0.0
            n = 0
            for o in self.fish:
                if o is f:
                    continue
                dx, dy = o["x"] - f["x"], o["y"] - f["y"]
                d2 = dx * dx + dy * dy
                if d2 < self.RANGE ** 2:
                    n += 1
                    cx += o["x"]
                    cy += o["y"]
                    avx += o["vx"]
                    avy += o["vy"]
                    if 0.01 < d2 < 49:
                        d = math.sqrt(d2)
                        ax -= dx / d * self.SEPARATE * (1 - d / 7)
                        ay -= dy / d * self.SEPARATE * (1 - d / 7)
            if n:
                ax += (cx / n - f["x"]) * self.COHESION + (avx / n - f["vx"]) * self.ALIGN
                ay += (cy / n - f["y"]) * self.COHESION + (avy / n - f["vy"]) * self.ALIGN
            if self.working:
                rx, ry = self.steer(f, lift, speed)
                wx, wy = self.steer_work(f, morph)
                sx, sy = rx + (wx - rx) * morph, ry + (wy - ry) * morph
            else:
                sx, sy = self.steer(f, lift, speed)
            f["vx"] += (ax + sx) * dt
            f["vy"] += (ay + sy) * dt
            f["vx"] *= 0.94 ** (dt * 30)
            f["vy"] *= 0.9 ** (dt * 30)
            cap = (speed * 1.6 + 50) if not self.working else 140 * self.rate
            sp = math.hypot(f["vx"], f["vy"])
            if sp > cap:
                f["vx"] *= cap / sp
                f["vy"] *= cap / sp
            if self.TRAIL:
                f["trail"].append((f["x"], f["y"]))
                del f["trail"][:-self.TRAIL]
            f["x"] += f["vx"] * dt
            f["y"] += f["vy"] * dt
            if f["x"] < EDGE:
                f["x"], f["vx"] = EDGE, abs(f["vx"])
            elif f["x"] > w - EDGE:
                f["x"], f["vx"] = w - EDGE, -abs(f["vx"])
            f["y"] = min(self.BASE - f["r"], max(self.TOP, f["y"]))
        self._lift_layers(dt)

    # -- the swell ----------------------------------------------------------------
    def x_of(self, p):
        return EDGE + (self.width - 2 * EDGE) * p / (self.POINTS - 1)

    def swell(self, layer, x):
        """The layer's own rolling swell, always moving."""
        t = self._t
        return (self.SWELL * (1 + 1.2 * self.level)) * (
            math.sin(x * 0.045 - t * 1.9 + layer * 0.7) * 0.7
            + math.sin(x * 0.11 + t * 1.3 + layer * 1.9) * 0.3)

    def extra_lift(self, layer, x):
        """A look's own shaping of the swell while working."""
        return 0.0

    def y_of(self, layer, p):
        x = self.x_of(p)
        return (self.BOTTOM - layer * self.GAP - self.u[layer][p] - self.swell(layer, x)
                - self.extra_lift(layer, x))

    def _lift_layers(self, dt):
        span = (self.width - 2 * EDGE) / (self.POINTS - 1)
        tops = [self.BASE] * self.POINTS
        for f in self.fish:
            p = int(round((f["x"] - EDGE) / span))
            for q in (p - 1, p, p + 1):
                if 0 <= q < self.POINTS:
                    tops[q] = min(tops[q], f["y"] - f["r"] - 1)
        n = self.POINTS
        for _ in range(2):
            h = dt / 2
            for layer in range(self.LAYERS):
                u, v = self.u[layer], self.uv[layer]
                acc = [-26.0 * u[i] + 700.0 / 9.0 * ((u[i - 1] if i else u[i]) + (u[i + 1] if i < n - 1 else u[i])
                                                     - 2 * u[i]) - 3.2 * v[i] for i in range(n)]
                for i in range(n):
                    v[i] += acc[i] * h
                    u[i] += v[i] * h
            for i in range(n):
                # The school under the bottom layer, then each layer under the next.
                y = self.y_of(0, i)
                if tops[i] < y:
                    # Move out of the fish's way, with a little momentum, so a
                    # school that keeps nudging does not pump the layer up.
                    push = y - tops[i]
                    self.u[0][i] += push * 0.4
                    self.uv[0][i] = max(self.uv[0][i], push * 5)
                for layer in range(1, self.LAYERS):
                    below, above = self.y_of(layer - 1, i), self.y_of(layer, i)
                    if above > below - self.GAP_MIN:
                        self.u[layer][i] += above - (below - self.GAP_MIN)
                        self.uv[layer][i] = max(self.uv[layer][i], self.uv[layer - 1][i] * 0.85)
                # The stack never leaves the top: clamp in screen space, from
                # the bottom layer up, so layers keep their order there too.
                for layer in range(self.LAYERS):
                    ceiling = self.TOP + (self.LAYERS - 1 - layer) * self.GAP_MIN
                    over = ceiling - self.y_of(layer, i)
                    if over > 0:
                        self.u[layer][i] -= over
                        self.uv[layer][i] = min(self.uv[layer][i], 0.0)
                    self.u[layer][i] = max(-4.0, self.u[layer][i])

    def ocean(self, layer, alpha=255, light=0.0) -> QColor:
        """Deep blue at the bottom of the stack to bright sea-glass at the top."""
        hue = 205 - layer * 9 + 10 * math.sin(self._t * 0.4)
        if current_palette().is_dark:
            color = QColor.fromHsv(int(hue) % 360, int(200 - layer * 18), int(min(255, 200 + layer * 11 + 30 * light)))
        else:
            color = QColor.fromHsv(int(hue) % 360, int(235 - layer * 12), int(170 + layer * 8))
        color.setAlpha(max(0, min(255, int(alpha))))
        return color

    def layer_path(self, layer):
        points = [QPointF(self.x_of(p), self.y_of(layer, p)) for p in range(self.POINTS)]
        path = QPainterPath(points[0])
        for a, b in zip(points, points[1:], strict=False):
            path.quadTo(a, QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2))
        path.lineTo(points[-1])
        return path

    def faded(self, layer, alpha, light=0.0):
        """A layer's colour fading out at both ends."""
        gradient = QLinearGradient(EDGE, 0, self.width - EDGE, 0)
        for stop, k in ((0.0, 0.0), (0.07, 1.0), (0.93, 1.0), (1.0, 0.0)):
            gradient.setColorAt(stop, self.ocean(layer, alpha * k, light))
        return gradient

    def draw_lines(self, painter, grow, strength=1.0):
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for layer in range(self.LAYERS):
            path = self.layer_path(layer)
            painter.setPen(QPen(self.faded(layer, 60 * grow * strength), 4.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawPath(path)
            painter.setPen(QPen(self.faded(layer, (150 + 20 * layer) * grow * strength), 1.6,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawPath(path)

    def draw_bands(self, painter, grow, light=0.0):
        """Filled swell bands; ``light`` (0-1, the voice) brightens the water."""
        for layer in range(self.LAYERS - 1, -1, -1):
            path = self.layer_path(layer)
            body = QPainterPath(path)
            body.lineTo(QPointF(self.width - EDGE, self.BASE))
            body.lineTo(QPointF(EDGE, self.BASE))
            body.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.fillPath(body, self.faded(layer, (34 + 6 * layer + 12 * light) * grow, light=light))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(self.faded(layer, (190 + 65 * light) * grow, light=1.0), 1.3 + 0.5 * light))
            painter.drawPath(path)

    # -- the fish -----------------------------------------------------------------
    #: The water's hue mid-stack (see ``ocean``).
    WATER_HUE = 190.0

    def fish_color(self, f, alpha=255) -> QColor:
        hue = (self.hue() + f["hue"]) % 360
        # Fish the colour of the water would vanish into it, so near its hue
        # they flash silver (dark theme) or deepen to navy (light theme).
        near = max(0.0, 1.0 - abs((hue - self.WATER_HUE + 180) % 360 - 180) / 90.0)
        if current_palette().is_dark:
            color = QColor.fromHsv(int(hue), int(200 - 170 * near), int(240 + 15 * near))
        else:
            color = QColor.fromHsv(int(hue), 235, int(205 - 85 * near))
        color.setAlpha(max(0, min(255, int(alpha))))
        return color

    def draw_balls(self, painter, grow):
        # Speaking lights the school up: each ball's halo grows and brightens.
        voice = self.voice()
        for f in self.fish:
            color = self.fish_color(f, 255 * grow)
            painter.setPen(Qt.PenStyle.NoPen)
            if voice > 0.05:
                halo = QColor(color)
                halo.setAlpha(int(70 * voice * grow))
                painter.setBrush(halo)
                painter.drawEllipse(QPointF(f["x"], f["y"]), f["r"] + 2.5 * voice, f["r"] + 2.5 * voice)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(f["x"], f["y"]), f["r"], f["r"])
            glow = QColor(color)
            glow.setAlpha(int((100 + 120 * voice) * grow))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(glow, 1))
            painter.drawEllipse(QPointF(f["x"], f["y"]), f["r"] + 1, f["r"] + 1)

    def draw_work(self, painter, morph, clock):
        self.draw(painter, 1.0)


class BaitBall(_School):
    """The school mills in a tight spinning ball that swells and rises with the voice."""

    COHESION = 0.6

    SQUASH = 2.6  # the ball is this much wider than tall
    #: How hard the whole ball swims for the depth the voice sets. The ring's
    #: own pull is mostly sideways (it is squashed), so on its own the ball
    #: drifted up and down too slowly to follow speech.
    DEPTH = 7.0

    def _swim(self, dt):
        self._depth = sum(f["y"] for f in self.fish) / len(self.fish)
        super()._swim(dt)

    def _ball(self, center_x, center_y, radius, spin, f):
        dx, dy = f["x"] - center_x, (f["y"] - center_y) * self.SQUASH
        d = math.hypot(dx, dy) + 0.01
        pull = (radius - d) * 6.0
        return dx / d * pull - dy / d * spin, (dy / d * pull + dx / d * spin) / self.SQUASH

    def steer(self, f, lift, speed):
        # Quiet, the ball lies low and small; speaking swells it, lifts it and
        # spins it up. The depth pull moves every fish alike, keeping the shape.
        depth = self.BASE - 4 - lift * 0.85
        ax, ay = self._ball(self.width / 2 + math.sin(self._t * 0.35) * 70, depth,
                            5 + 26 * self.level, 50 + 230 * self.level, f)
        return ax, ay + (depth - self._depth) * self.DEPTH

    def steer_work(self, f, morph):
        # Pulls into the centre, tightens, and spins faster as the work runs.
        return self._ball(self.width / 2, self.BASE - 15, 7.0, 150 + 110 * self.rate, f)

    def extra_lift(self, layer, x):
        if not self.working:
            return 0.0
        dome = math.exp(-((x - self.width / 2) / 46.0) ** 2)
        return dome * (5.0 + 1.5 * math.sin(self._work_t * 5.0 * self.rate)) * self.morph()

    def draw(self, painter, grow):
        self.draw_balls(painter, grow)
        self.draw_lines(painter, grow)


class RealFish(_School):
    """Little teardrop fish with wagging tails, each pointing the way it swims."""

    FISH = 44

    def steer_work(self, f, morph):
        # A slow wheel round the centre, like a school circling.
        cx, cy = self.width / 2, self.BASE - 13
        dx, dy = (f["x"] - cx) / 1.0, (f["y"] - cy) * 3.0
        d = math.hypot(dx, dy) + 0.01
        pull = (40.0 - d) * 3.0
        spin = 70 + 50 * self.rate
        return dx / d * pull - dy / d * spin, (dy / d * pull + dx / d * spin) / 3.0

    def draw_fish(self, painter, grow):
        # Speaking, the school swims harder: bigger, quicker tails.
        voice = self.voice()
        size = 1.0 + 0.3 * voice
        for f in self.fish:
            angle = math.atan2(f["vy"], f["vx"] if abs(f["vx"]) > 0.1 else 0.1)
            r = f["r"] * size
            length = 2.4 + r * 1.6
            painter.save()
            painter.translate(f["x"], f["y"])
            painter.rotate(math.degrees(angle))
            body = QPainterPath()
            body.moveTo(length, 0)
            body.quadTo(0, -r * 0.9, -length * 0.6, 0)
            body.quadTo(0, r * 0.9, length, 0)
            wag = math.sin(self._t * (10 + 14 * voice) + f["phase"] * 5) * r * (0.45 + 0.3 * voice)
            tail = QPainterPath()
            tail.moveTo(-length * 0.5, 0)
            tail.lineTo(-length * 1.05, -r * 0.8 + wag)
            tail.lineTo(-length * 1.05, r * 0.8 + wag)
            tail.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.fish_color(f, 240 * grow))
            painter.drawPath(body)
            painter.setBrush(self.fish_color(f, 170 * grow))
            painter.drawPath(tail)
            painter.restore()

    def draw(self, painter, grow):
        self.draw_bands(painter, grow, light=self.voice())
        self.draw_fish(painter, grow)

    def draw_work(self, painter, morph, clock):
        self.draw(painter, 1.0)
        # Light shimmering across the water, like caustics.
        x = EDGE + (self.width - 2 * EDGE) * ((clock * 0.8) % 1.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for layer in range(self.LAYERS):
            shimmer = QLinearGradient(x - 50, 0, x + 50, 0)
            shimmer.setColorAt(0.0, self.ocean(layer, 0, 1.0))
            shimmer.setColorAt(0.5, self.ocean(layer, 230 * morph, 1.0))
            shimmer.setColorAt(1.0, self.ocean(layer, 0, 1.0))
            painter.setPen(QPen(shimmer, 2.0))
            painter.drawPath(self.layer_path(layer))


class DeepGlow(_School):
    """Bioluminescent fish leaving light trails under faint swell lines, like the deep at night."""

    TRAIL = 7
    PING_S = 0.9

    def fish_color(self, f, alpha=255):
        hue = 160 + f["hue"] * 0.6 + 15 * math.sin(self._t + f["phase"])
        if current_palette().is_dark:
            color = QColor.fromHsv(int(hue) % 360, 150, 255)
        else:
            color = QColor.fromHsv(int(hue) % 360, 235, 190)
        color.setAlpha(max(0, min(255, int(alpha))))
        return color

    def steer_work(self, f, morph):
        # A whirl: each fish keeps to its own ring round the centre and spirals.
        cx, cy = self.width / 2, self.BASE - 15
        dx, dy = f["x"] - cx, (f["y"] - cy) * 2.0
        d = math.hypot(dx, dy) + 0.01
        ring = 6 + 16 * (f["phase"] / math.tau)
        pull = (ring - d) * 7.0
        spin = 120 + 100 * self.rate
        return dx / d * pull - dy / d * spin, (dy / d * pull + dx / d * spin) / 2.0

    def draw_glow(self, painter, grow):
        for f in self.fish:
            trail = f["trail"]
            for k in range(1, len(trail)):
                share = k / len(trail)
                painter.setPen(QPen(self.fish_color(f, 110 * share * grow), f["r"] * share,
                                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(QPointF(*trail[k - 1]), QPointF(*trail[k]))
            twinkle = 0.65 + 0.35 * math.sin(self._t * 6 + f["phase"] * 3)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.fish_color(f, 70 * twinkle * grow))
            painter.drawEllipse(QPointF(f["x"], f["y"]), f["r"] + 2.5, f["r"] + 2.5)
            painter.setBrush(self.fish_color(f, 255 * twinkle * grow))
            painter.drawEllipse(QPointF(f["x"], f["y"]), f["r"] * 0.8, f["r"] * 0.8)

    def draw(self, painter, grow):
        self.draw_glow(painter, grow)
        self.draw_lines(painter, grow, strength=0.6)

    def draw_work(self, painter, morph, clock):
        center = QPointF(self.width / 2, self.BASE - 15)
        # Sonar pings ripple out through the dimmed water.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for k in range(3):
            age = ((clock / self.PING_S) + k / 3) % 1.0
            painter.setPen(QPen(self.fish_color(self.fish[0], 170 * (1 - age) ** 2 * morph), 1.2))
            painter.drawEllipse(center, 8 + 120 * age, 4 + 28 * age)
        self.draw_glow(painter, 1.0)
        self.draw_lines(painter, 1.0, strength=0.6 - 0.35 * morph)


class Fizz(Visual):
    """Glasses that fill with fizz: every bubble that reaches the surface lifts the level.

    The voice sets how many bubbles launch and how fast they race; the level
    drains back down in pauses. Finish: the levels roll from glass to
    glass like a filling wave while the bubbles keep fizzing.
    """

    particles = False
    LANES = 9
    BASE = 50.0
    TOP = 8.0
    KICK = 15.0
    GRAVITY = 80.0
    #: Extra drain per pixel of fizz: quiet speech settles about a quarter
    #: full, ordinary speech about three quarters, a raised voice at the brim.
    LEAK = 6.0
    #: How long a slosh rings on before the level settles.
    SLOSH_S = 0.35
    MAX_H = 38.0

    def reset(self):
        self.h = [0.0] * self.LANES
        self.v = [0.0] * self.LANES
        self.bubbles: list = []
        self.splashes: list = []
        self._t = 0.0
        self._carry = 0.0
        self.rng = random.Random(11)
        self._work_from: list = []

    def pitch(self):
        return (self.width - 2 * EDGE) / self.LANES

    def lane_x(self, i):
        return EDGE + self.pitch() * (i + 0.5)

    def _spawn(self, rate, speed, dt):
        self._carry += rate * dt
        while self._carry >= 1:
            self._carry -= 1
            lane = self.rng.randrange(self.LANES)
            self.bubbles.append({
                "lane": lane, "x": self.lane_x(lane) + self.rng.uniform(-0.3, 0.3) * (self.pitch() - 8),
                "y": self.BASE, "vy": -speed * self.rng.uniform(0.8, 1.2), "r": self.rng.uniform(1.4, 3.0),
            })

    def _rise(self, dt, lift=True):
        alive = []
        for b in self.bubbles:
            b["y"] += b["vy"] * dt
            surface = self.BASE - self.h[b["lane"]]
            if b["y"] - b["r"] <= surface:
                if lift:
                    self.v[b["lane"]] += self.KICK * (abs(b["vy"]) / 300) * (b["r"] / 2.4)
                self.splashes.append((self._t, b["x"], surface))
            elif b["y"] > -10:
                alive.append(b)
        self.bubbles = alive
        self.splashes = [s for s in self.splashes if self._t - s[0] < 0.3]

    def advance(self, dt):
        self._t += dt
        level = self.ribbon.shown
        self._spawn(10 + 230 * level ** 1.5, 110 + 320 * level, dt)
        self._rise(dt)
        for i in range(self.LANES):
            # The fuller a glass, the harder it drains, so each loudness has
            # its own level to slosh around instead of every word filling it.
            self.v[i] -= (self.GRAVITY + self.LEAK * self.h[i]) * dt
            self.v[i] *= math.exp(-dt / self.SLOSH_S)
            self.h[i] = max(0.0, min(self.MAX_H, self.h[i] + self.v[i] * dt))
            if self.h[i] in (0.0, self.MAX_H):
                self.v[i] = 0.0

    def begin_work(self):
        super().begin_work()
        self._work_from = list(self.h)

    def advance_work(self, dt, rate):
        self._t += dt
        self._spawn(40 * rate, 180 * rate, dt)
        self._rise(dt, lift=False)

    def _draw_glasses(self, painter, levels, grow, foam_speed=9.0):
        w = self.pitch() - 8
        hue = self.hue()
        for i, h in enumerate(levels):
            x = self.lane_x(i)
            glass = QRectF(x - w / 2, self.TOP, w, self.BASE - self.TOP)
            level = self.BASE - h
            fill = QLinearGradient(0, level, 0, self.BASE)
            fill.setColorAt(0.0, hue_color(hue + i / self.LANES * 60, 190 * grow))
            fill.setColorAt(1.0, hue_color(hue + i / self.LANES * 60, 70 * grow))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRect(QRectF(glass.left() + 1, level, w - 2, self.BASE - level))
            for k in range(4):
                foam_x = glass.left() + 3 + k * (w - 6) / 3
                painter.setBrush(hue_color(hue + 40, 200 * grow))
                painter.drawEllipse(QPointF(foam_x, level - 1), 1.8 + 0.6 * math.sin(self._t * foam_speed + k + i), 1.6)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(hue_color(hue + i / self.LANES * 60, 150 * grow), 1))
            painter.drawRoundedRect(glass, 3, 3)

    def _draw_bubbles(self, painter, grow):
        hue = self.hue()
        painter.setPen(Qt.PenStyle.NoPen)
        for b in self.bubbles:
            if b["y"] < self.BASE - self.h[b["lane"]] - 1:
                continue  # above the surface it has already popped
            painter.setBrush(hue_color(hue + 30, 230 * grow))
            painter.drawEllipse(QPointF(b["x"], b["y"]), b["r"], b["r"])
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for start, x, y in self.splashes:
            age = (self._t - start) / 0.3
            painter.setPen(QPen(hue_color(hue + 40, 200 * (1 - age) * grow), 1))
            painter.drawEllipse(QPointF(x, y), 2 + 5 * age, 1 + 2 * age)

    def draw(self, painter, grow):
        self._draw_glasses(painter, self.h, grow)
        self._draw_bubbles(painter, grow)

    def draw_work(self, painter, morph, clock):
        # The levels ease from where they were into a wave rolling across.
        start = self._work_from or self.h
        wave = [10 + 20 * (0.5 + 0.5 * math.sin(math.tau * (i / self.LANES - clock * 0.7)))
                for i in range(self.LANES)]
        self.h = [a + (b - a) * morph for a, b in zip(start, wave, strict=True)]
        self._draw_glasses(painter, self.h, 1.0, foam_speed=9.0 + 6.0 * morph)
        self._draw_bubbles(painter, 1.0)
