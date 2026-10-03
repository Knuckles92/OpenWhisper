"""Physics-based waveform particles."""
import math
import random
import time
from typing import Dict, Any, List, Optional
from PyQt6.QtGui import QPainter, QColor, QPen, QFont
from PyQt6.QtCore import QRect, QRectF, Qt
from ui_qt.utils.palette import current_palette, token_color


def round_pen(color: QColor, width: float) -> QPen:
    """Pen with round caps/joins so drawn glyph strokes look polished."""
    return QPen(
        color, width, Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
    )


class Particle:
    def __init__(
        self,
        x: float,
        y: float,
        vx: float = 0,
        vy: float = 0,
        hue: Optional[float] = None,
    ):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.life = 1.0
        self.size = random.uniform(1.5, 4.0)
        self.color_hue = random.uniform(0, 360) if hue is None else hue

    def update(self, dt: float, gravity: float = 0, damping: float = 0.99):
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.vy += gravity * dt

        frame_damping = damping ** (dt * 30.0)
        self.vx *= frame_damping
        self.vy *= frame_damping

        self.life -= dt * 0.5
        return self.life > 0

    def get_qcolor(self, base_hue: float = None) -> QColor:
        hue = base_hue if base_hue is not None else self.color_hue
        if current_palette().is_dark:
            # Dying particles darken into the dark pill.
            return QColor.fromHsv(int(hue) % 360, 200, int(self.life * 230 + 25))
        # On a light pill they would darken into soot, so they fade out
        # instead, and run more saturated to hold against white.
        return QColor.fromHsv(
            int(hue) % 360, 235, 205, max(0, min(255, int(self.life * 255)))
        )

    def get_fading_color(self) -> QColor:
        """Colour that fades out by alpha on both themes.

        The overlay's status-glyph swarms use this so their particles thin out
        around the glyph instead of darkening into the pill.
        """
        alpha = int(255 * self.life)
        if current_palette().is_dark:
            return QColor.fromHsv(int(self.color_hue) % 360, 200, 230, alpha)
        return QColor.fromHsv(int(self.color_hue) % 360, 235, 205, alpha)


class ParticleStyle:
    """Particle waveform driven by audio energy."""

    def __init__(self, width: int, height: int, config: Dict[str, Any]):
        self.width = width
        self.height = height

        self.animation_time = 0.0
        self.audio_levels: List[float] = []
        self._canceling_start_time: Optional[float] = None

        self.max_particles = config.get('max_particles', 500)
        self.emission_rate = config.get('emission_rate', 100)

        self.gravity = config.get('gravity', 20)
        self.damping = config.get('damping', 0.98)
        self.wind_strength = config.get('wind_strength', 5)
        self.audio_response = config.get('audio_response', 1.5)

        self.glow_effect = config.get('glow_effect', True)

        self.turbulence_strength = config.get('turbulence_strength', 10)
        self.color_shift_speed = config.get('color_shift_speed', 50)

        self.particles: List[Particle] = []
        self.cancel_particles: List[Particle] = []
        self._cancel_initialized = False
        self._last_cancel_progress = 1.0
        self._last_cancel_update: Optional[float] = None
        self._simulation_remainder = 0.0
        self._emission_remainder = 0.0
        self._simulation_state = "idle"
        self._cancel_elapsed = 0.0

    def update_audio_levels(self, levels: List[float]):
        self.audio_levels = levels.copy() if levels else []

    def update_animation_time(self, delta_time: float):
        """Advance animation time by ``delta_time`` seconds."""
        self.animation_time += delta_time

    def get_cancellation_progress(self) -> float:
        from config import config
        duration = config.CANCELLATION_ANIMATION_DURATION_MS / 1000.0
        return min(1.0, self._cancel_elapsed / max(duration, 0.001))

    def set_canceling_start_time(self, start_time: float):
        self._canceling_start_time = start_time
        self._cancel_elapsed = 0.0
        self._cancel_initialized = False

    def advance(self, state: str, delta_time: float) -> None:
        """Advance physics once per timer tick, independently of repaint count.

        Fixed substeps keep emission and motion consistent across display frame
        rates. Clamp stalls so a sleeping/hidden window does not emit a burst.
        """
        delta_time = max(0.0, min(0.1, delta_time))
        if state != self._simulation_state:
            self._simulation_state = state
            self._emission_remainder = 0.0
            if state == "canceling":
                self._init_cancel_particles(QRect(0, 0, self.width, self.height))
        self._simulation_remainder += delta_time
        step = 1.0 / 120.0
        while self._simulation_remainder + 1e-12 >= step:
            self._simulation_remainder -= step
            self.animation_time += step
            if state in ("recording", "streaming"):
                self._advance_recording(step)
            elif state == "processing":
                self._advance_processing(step)
            elif state == "transcribing":
                self._advance_transcribing(step)
            elif state == "canceling":
                self._cancel_elapsed += step
                self._update_cancel_particles(step)
                if self.get_cancellation_progress() >= 1.0:
                    self.cancel_particles.clear()

    def _emission_count(self, rate: float, dt: float) -> int:
        self._emission_remainder += rate * dt
        count = int(self._emission_remainder + 1e-12)
        self._emission_remainder -= count
        return count

    def _advance_recording(self, dt: float):
        audio_energy = sum(self.audio_levels) / len(self.audio_levels) if self.audio_levels else 0.0

        emission_multiplier = 1.0 + audio_energy * self.audio_response
        particles_to_emit = self._emission_count(self.emission_rate * emission_multiplier, dt)

        self._emit_audio_particles(particles_to_emit, audio_energy)

        self._update_particles(dt, audio_energy)


    def _advance_processing(self, dt: float):
        """Draw swirling particle vortex."""
        center_x = self.width // 2
        center_y = self.height // 2 - 5

        vortex_particles = self._emission_count(120.0, dt)
        for i in range(vortex_particles):
            angle = (i / vortex_particles) * 2 * math.pi + self.animation_time * 2
            radius = 30 + 10 * math.sin(self.animation_time * 3)

            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)

            vx = -math.sin(angle) * 50
            vy = math.cos(angle) * 50

            particle = Particle(x, y, vx, vy)
            particle.color_hue = (angle * 180 / math.pi + self.animation_time * 50) % 360
            self.particles.append(particle)

        self._update_particles(dt, 0.5, vortex_mode=True)

    def _advance_transcribing(self, dt: float):
        """Draw particles converging to center."""

        particles_per_frame = self._emission_count(90.0, dt)
        for _ in range(particles_per_frame):
            edge = random.randint(0, 3)
            if edge == 0:
                x = random.uniform(0, self.width)
                y = -10
            elif edge == 1:
                x = self.width + 10
                y = random.uniform(0, self.height)
            elif edge == 2:
                x = random.uniform(0, self.width)
                y = self.height + 10
            else:
                x = -10
                y = random.uniform(0, self.height)

            center_x = self.width // 2
            center_y = self.height // 2
            angle = math.atan2(center_y - y, center_x - x)

            speed = random.uniform(200, 400)
            vx = math.cos(angle) * speed
            vy = math.sin(angle) * speed

            particle = Particle(x, y, vx, vy)
            particle.color_hue = random.uniform(0, 360)
            self.particles.append(particle)

        self._update_particles(dt, 0.3, converge_mode=True)

    def draw_recording_state(self, painter: QPainter, rect: QRect, message: str = "Recording..."):
        self._draw_particles(painter)
        self._draw_text(painter, rect, message)

    def draw_processing_state(self, painter: QPainter, rect: QRect, message: str = "Processing..."):
        self._draw_particles(painter)
        self._draw_text(painter, rect, message)

    def draw_transcribing_state(self, painter: QPainter, rect: QRect, message: str = "Transcribing..."):
        self._draw_particles(painter)
        self._draw_text(painter, rect, message)

    def draw_canceling_state(self, painter: QPainter, rect: QRect, message: str = "Canceled"):
        progress = self.get_cancellation_progress()
        self._draw_cancel_particles(painter, progress)
        center_x = rect.width() // 2
        center_y = rect.height() // 2 - 5
        size = int(26 * (1.0 - 0.6 * progress))
        alpha = max(0, int(255 * (1.0 - progress)))
        painter.setPen(round_pen(token_color("danger", alpha), 3))
        painter.drawLine(center_x - size, center_y - size, center_x + size, center_y + size)
        painter.drawLine(center_x + size, center_y - size, center_x - size, center_y + size)
        painter.setPen(token_color("overlay-text", alpha))
        painter.setFont(QFont("Segoe UI", 10))
        painter.drawText(QRect(0, rect.height() - 25, rect.width(), 20),
                         Qt.AlignmentFlag.AlignCenter, message)

    def _emit_audio_particles(self, count: int, audio_energy: float):
        for _ in range(min(count, self.max_particles - len(self.particles))):
            x = random.uniform(20, self.width - 20)
            y = self.height - 30

            vx = random.uniform(-30, 30) * (1 + audio_energy)
            vy = random.uniform(-80, -40) * (1 + audio_energy * 0.5)

            particle = Particle(x, y, vx, vy)
            particle.color_hue = (self.animation_time * self.color_shift_speed +
                                random.uniform(0, 60)) % 360
            self.particles.append(particle)

    def _update_particles(self, dt: float, audio_energy: float = 0.0,
                         vortex_mode: bool = False, converge_mode: bool = False):
        center_x = self.width // 2
        center_y = self.height // 2

        alive_particles = []

        for particle in self.particles:
            if vortex_mode:
                dx = particle.x - center_x
                dy = particle.y - center_y
                distance = math.sqrt(dx*dx + dy*dy)

                if distance > 0:
                    radial_force = -50 / (distance + 1)
                    particle.vx += (dx / distance) * radial_force * dt
                    particle.vy += (dy / distance) * radial_force * dt

                    tangent_force = 100
                    particle.vx += (-dy / distance) * tangent_force * dt
                    particle.vy += (dx / distance) * tangent_force * dt

                if particle.update(dt, 0, 0.95):
                    alive_particles.append(particle)

            elif converge_mode:
                dx = center_x - particle.x
                dy = center_y - particle.y
                distance = math.sqrt(dx*dx + dy*dy)

                if distance > 5:
                    nx = dx / distance
                    ny = dy / distance

                    attraction = 15000 / (distance + 10) + 500

                    swirl = 300

                    particle.vx += (nx * attraction - ny * swirl) * dt
                    particle.vy += (ny * attraction + nx * swirl) * dt

                    particle.vx *= 0.9 ** (dt * 30.0)
                    particle.vy *= 0.9 ** (dt * 30.0)
                else:
                    particle.life -= dt * 5.0

                if particle.update(dt, 0, 1.0):
                    alive_particles.append(particle)

            else:
                turbulence_multiplier = 1.0 + audio_energy * 2
                turbulence_x = (math.sin(self.animation_time * 3 + particle.x * 0.1) *
                              self.turbulence_strength * turbulence_multiplier)
                turbulence_y = (math.cos(self.animation_time * 2.5 + particle.y * 0.1) *
                              self.turbulence_strength * turbulence_multiplier * 0.7)

                particle.vx += turbulence_x * dt
                particle.vy += turbulence_y * dt

                wind_x = math.sin(self.animation_time) * self.wind_strength
                particle.vx += wind_x * dt

                if particle.x <= 0 or particle.x >= self.width:
                    particle.vx *= -0.8
                    particle.x = max(0, min(self.width, particle.x))

                if particle.y >= self.height - 25:
                    particle.vy *= -0.6
                    particle.y = self.height - 25

                if particle.update(dt, self.gravity, self.damping):
                    alive_particles.append(particle)

        self.particles = alive_particles
        if len(self.particles) > self.max_particles:
            self.particles = self.particles[-self.max_particles:]

    def _draw_particles(self, painter: QPainter):
        painter.setPen(Qt.PenStyle.NoPen)

        for particle in self.particles:
            color = particle.get_qcolor()
            painter.setBrush(color)

            size = particle.size * particle.life
            painter.drawEllipse(QRectF(particle.x - size, particle.y - size, size * 2, size * 2))

            if self.glow_effect and particle.life > 0.5:
                glow_color = QColor(color)
                glow_color.setAlpha(100)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(glow_color, 1))
                glow_size = size + 1
                painter.drawEllipse(QRectF(particle.x - glow_size, particle.y - glow_size, glow_size * 2, glow_size * 2))
                painter.setPen(Qt.PenStyle.NoPen)

    def _draw_text(self, painter: QPainter, rect: QRect, message: str):
        painter.setPen(token_color("overlay-text"))
        font = QFont("Segoe UI", 10, QFont.Weight.Bold)
        painter.setFont(font)
        text_rect = QRect(0, rect.height() - 25, rect.width(), 20)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, message)

    def _init_cancel_particles(self, rect: QRect):
        self.cancel_particles = []
        center_x = rect.width() // 2
        center_y = rect.height() // 2 - 5

        for _ in range(70):
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(160, 320)
            vx = math.cos(angle) * speed
            vy = math.sin(angle) * speed

            particle = Particle(center_x, center_y, vx, vy)
            particle.size = random.uniform(2.5, 5.0)
            particle.color_hue = random.uniform(0, 40)
            self.cancel_particles.append(particle)

        self._cancel_initialized = True
        self._last_cancel_progress = 0.0
        self._last_cancel_update = time.time()

    def _cancel_dt(self) -> float:
        now = time.time()
        if self._last_cancel_update is None:
            self._last_cancel_update = now
            return 1 / 30

        dt = now - self._last_cancel_update
        self._last_cancel_update = now
        return max(0.0, min(0.05, dt))

    def _update_cancel_particles(self, dt: float):
        alive = []
        for particle in self.cancel_particles:
            particle.vx += random.uniform(-25, 25) * dt
            particle.vy += random.uniform(-25, 25) * dt

            if particle.update(dt, gravity=0, damping=0.92):
                alive.append(particle)

        self.cancel_particles = alive

    def _draw_cancel_particles(self, painter: QPainter, progress: float):
        painter.setPen(Qt.PenStyle.NoPen)

        for particle in self.cancel_particles:
            color = particle.get_qcolor(base_hue=particle.color_hue)
            alpha = int(255 * particle.life * (1.0 - progress * 0.7))
            if alpha <= 0:
                continue

            color.setAlpha(alpha)
            size = particle.size * (1.0 + 0.8 * (1.0 - progress))
            painter.setBrush(color)
            painter.drawEllipse(QRectF(particle.x - size, particle.y - size, size * 2, size * 2))

            if self.glow_effect and alpha > 80:
                glow_color = QColor(color)
                glow_color.setAlpha(int(alpha * 0.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(glow_color, 1))
                glow_size = size + 2
                painter.drawEllipse(QRectF(particle.x - glow_size, particle.y - glow_size, glow_size * 2, glow_size * 2))
                painter.setPen(Qt.PenStyle.NoPen)
