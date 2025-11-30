"""
Particle Storm style - physics-based particles that respond to audio levels.
PyQt6 version.
"""
import math
import random
import time
from typing import Dict, Any, List, Optional, Tuple
from PyQt6.QtGui import QPainter, QColor, QPen, QFont, QBrush
from PyQt6.QtCore import QRect, QRectF, Qt
from .base_style import BaseWaveformStyle


class Particle:
    """Individual particle with physics properties."""

    def __init__(self, x: float, y: float, vx: float = 0, vy: float = 0):
        self.x = x
        self.y = y
        self.vx = vx  # velocity x
        self.vy = vy  # velocity y
        self.life = 1.0  # life from 1.0 to 0.0
        self.max_life = 1.0
        self.size = random.uniform(1.5, 4.0)
        self.color_hue = random.uniform(0, 360)
        self.birth_time = 0

    def update(self, dt: float, gravity: float = 0, damping: float = 0.99):
        """Update particle physics."""
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.vy += gravity * dt

        # Apply damping
        self.vx *= damping
        self.vy *= damping

        # Reduce life
        self.life -= dt * 0.5
        return self.life > 0

    def get_qcolor(self, base_hue: float = None) -> QColor:
        """Get particle color as QColor."""
        hue = base_hue if base_hue is not None else self.color_hue
        saturation = 200  # 0-255
        value = int(self.life * 230 + 25)  # Brightness based on life

        # QColor.fromHsv uses 0-359 for hue, 0-255 for s/v
        return QColor.fromHsv(int(hue) % 360, saturation, value)


class ParticleStyle(BaseWaveformStyle):
    """Particle storm style with physics-based particles."""

    def __init__(self, width: int, height: int, config: Dict[str, Any]):
        super().__init__(width, height, config)

        self._display_name = "Particle Storm"
        self._description = "Physics-based particles responding to audio"

        # Particle system settings
        self.max_particles = config.get('max_particles', 500)
        self.emission_rate = config.get('emission_rate', 100)
        self.particle_life = config.get('particle_life', 2.0)

        # Physics settings
        self.gravity = config.get('gravity', 20)
        self.damping = config.get('damping', 0.98)
        self.wind_strength = config.get('wind_strength', 5)
        self.audio_response = config.get('audio_response', 1.5)

        # Visual settings
        self.bg_color = config.get('bg_color', '#0a0a0a')
        self.text_color = config.get('text_color', '#ffffff')
        self.glow_effect = config.get('glow_effect', True)

        # Animation settings
        self.turbulence_strength = config.get('turbulence_strength', 10)
        self.color_shift_speed = config.get('color_shift_speed', 50)

        # Particle system state
        self.particles: List[Particle] = []
        self.last_frame_time = 0
        self.cancel_particles: List[Particle] = []
        self._cancel_initialized = False
        self._last_cancel_progress = 1.0
        self._last_cancel_update: Optional[float] = None

    def _hex_to_qcolor(self, hex_color: str) -> QColor:
        """Convert hex color string to QColor."""
        if not hex_color.startswith('#'):
            return QColor(hex_color)
        hex_color = hex_color.lstrip('#')
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return QColor(r, g, b)

    def draw_recording_state(self, painter: QPainter, rect: QRect, message: str = "Recording..."):
        """Draw particles responding to audio levels."""
        dt = 1/30  # Approximate dt since we don't have exact frame delta here easily without passing it
        # Actually base_style has update_animation_time which updates self.animation_time
        # We can use a fixed dt for physics stability or calculate it if we tracked last time
        
        # Calculate audio energy
        audio_energy = sum(self.audio_levels) / len(self.audio_levels) if self.audio_levels else 0.0

        # Emit new particles based on audio
        emission_multiplier = 1.0 + audio_energy * self.audio_response
        particles_to_emit = int(self.emission_rate * emission_multiplier * dt)

        self._emit_audio_particles(particles_to_emit, audio_energy)

        # Update and draw particles
        self._update_particles(dt, audio_energy)
        self._draw_particles(painter)

        # Draw status text
        self._draw_text(painter, rect, message)

    def draw_processing_state(self, painter: QPainter, rect: QRect, message: str = "Processing..."):
        """Draw swirling particle vortex."""
        dt = 1/30
        center_x = rect.width() // 2
        center_y = rect.height() // 2 - 5

        # Emit particles in a vortex pattern
        vortex_particles = 4
        for i in range(vortex_particles):
            angle = (i / vortex_particles) * 2 * math.pi + self.animation_time * 2
            radius = 30 + 10 * math.sin(self.animation_time * 3)

            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)

            # Velocity tangent to circle
            vx = -math.sin(angle) * 50
            vy = math.cos(angle) * 50

            particle = Particle(x, y, vx, vy)
            particle.color_hue = (angle * 180 / math.pi + self.animation_time * 50) % 360
            self.particles.append(particle)

        self._update_particles(dt, 0.5, vortex_mode=True)
        self._draw_particles(painter)
        self._draw_text(painter, rect, message)

    def draw_transcribing_state(self, painter: QPainter, rect: QRect, message: str = "Transcribing..."):
        """Draw particles converging to center."""
        dt = 1/30
        
        # Emit particles from edges - multiple per frame for density
        particles_per_frame = random.randint(2, 4)
        for _ in range(particles_per_frame):
            # Pick a random edge
            edge = random.randint(0, 3)
            if edge == 0:   # Top
                x = random.uniform(0, self.width)
                y = -10
            elif edge == 1: # Right
                x = self.width + 10
                y = random.uniform(0, self.height)
            elif edge == 2: # Bottom
                x = random.uniform(0, self.width)
                y = self.height + 10
            else:           # Left
                x = -10
                y = random.uniform(0, self.height)
            
            # Initial velocity towards center
            center_x = self.width // 2
            center_y = self.height // 2
            angle = math.atan2(center_y - y, center_x - x)
            
            # Faster initial speed for rapid convergence
            speed = random.uniform(200, 400)
            vx = math.cos(angle) * speed
            vy = math.sin(angle) * speed
            
            particle = Particle(x, y, vx, vy)
            # Full spectrum of colors for transcription
            particle.color_hue = random.uniform(0, 360) 
            self.particles.append(particle)

        self._update_particles(dt, 0.3, converge_mode=True)
        self._draw_particles(painter)
        self._draw_text(painter, rect, message)

    def draw_canceling_state(self, painter: QPainter, rect: QRect, message: str = "Cancelled"):
        """Draw canceling state with a quick red burst."""
        progress = self.get_cancellation_progress()

        # Reinitialize burst when a new cancel starts (progress resets)
        if (progress < self._last_cancel_progress - 0.2) or not self._cancel_initialized:
            self._init_cancel_particles(rect)

        dt = self._cancel_dt()
        self._update_cancel_particles(dt)
        self._draw_cancel_particles(painter, progress)

        # Fading red X, similar in feel to other states
        center_x = rect.width() // 2
        center_y = rect.height() // 2 - 5
        size = int(26 * (1.0 - 0.6 * progress))
        alpha = max(0, int(255 * (1.0 - progress)))

        painter.setPen(QPen(QColor(239, 68, 68, alpha), 3))
        painter.drawLine(center_x - size, center_y - size, center_x + size, center_y + size)
        painter.drawLine(center_x + size, center_y - size, center_x - size, center_y + size)

        # Status text with fade
        painter.setPen(QColor(255, 255, 255, alpha))
        font = QFont("Segoe UI", 10)
        painter.setFont(font)
        text_rect = QRect(0, rect.height() - 25, rect.width(), 20)
        painter.drawText(text_rect, 0x0004 | 0x0080, message)

        # Cleanup when animation finishes
        if progress >= 1.0:
            self.cancel_particles.clear()
            self._cancel_initialized = False
            self._last_cancel_progress = 1.0
            self._last_cancel_update = None
        else:
            self._last_cancel_progress = progress

    def draw_stt_enable_state(self, painter: QPainter, rect: QRect, message: str = "STT Enabled"):
        """Draw STT enable state."""
        self._draw_text(painter, rect, message)

    def draw_stt_disable_state(self, painter: QPainter, rect: QRect, message: str = "STT Disabled"):
        """Draw STT disable state."""
        self._draw_text(painter, rect, message)

    def _emit_audio_particles(self, count: int, audio_energy: float):
        """Emit particles based on audio energy."""
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
                         vortex_mode: bool = False, stream_mode: bool = False, converge_mode: bool = False):
        """Update all particles."""
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
                # Rapid attraction to center with swirl
                dx = center_x - particle.x
                dy = center_y - particle.y
                distance = math.sqrt(dx*dx + dy*dy)
                
                if distance > 5:
                    # Normalized direction
                    nx = dx / distance
                    ny = dy / distance
                    
                    # Strong attraction force that increases closer to center
                    # Base pull + inverse distance pull for snap
                    attraction = 15000 / (distance + 10) + 500
                    
                    # Swirl force (tangential) for "beautiful" effect
                    swirl = 300
                    
                    particle.vx += (nx * attraction - ny * swirl) * dt
                    particle.vy += (ny * attraction + nx * swirl) * dt
                    
                    # Damping to keep it smooth but fast
                    particle.vx *= 0.9
                    particle.vy *= 0.9
                else:
                    # Reached center, vanish quickly
                    particle.life -= dt * 5.0
                
                if particle.update(dt, 0, 1.0):
                    alive_particles.append(particle)

            elif stream_mode:
                turbulence_x = math.sin(self.animation_time * 2 + particle.y * 0.1) * self.turbulence_strength
                turbulence_y = math.cos(self.animation_time * 1.5 + particle.x * 0.1) * self.turbulence_strength * 0.5
                
                particle.vx += turbulence_x * dt
                particle.vy += turbulence_y * dt
                
                if (particle.x < -10 or particle.x > self.width + 10 or 
                    particle.y < -10 or particle.y > self.height + 10):
                    continue
                    
                if particle.update(dt, self.gravity * 0.3, self.damping):
                    alive_particles.append(particle)
            
            else:
                # Normal mode
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
        
        # Filter out any invalid particles and ensure we have Particle objects
        valid_particles = []
        for p in alive_particles:
            if isinstance(p, Particle) and hasattr(p, 'x') and hasattr(p, 'y'):
                valid_particles.append(p)
        
        self.particles = valid_particles
        if len(self.particles) > self.max_particles:
            self.particles = self.particles[-self.max_particles:]

    def _draw_particles(self, painter: QPainter):
        """Draw all particles."""
        painter.setPen(Qt.PenStyle.NoPen)
        
        for particle in self.particles:
            try:
                # Ensure particle is a Particle object, not a tuple or list
                if not hasattr(particle, 'x') or not hasattr(particle, 'y'):
                    continue
                    
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
            except (AttributeError, TypeError) as e:
                # Skip invalid particles
                import logging
                logging.debug(f"Skipping invalid particle: {e}")
                continue

    def _draw_text(self, painter: QPainter, rect: QRect, message: str):
        """Draw status text."""
        painter.setPen(self._hex_to_qcolor(self.text_color))
        font = QFont("Segoe UI", 10, QFont.Weight.Bold)
        painter.setFont(font)
        text_rect = QRect(0, rect.height() - 25, rect.width(), 20)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, message)

    def _init_cancel_particles(self, rect: QRect):
        """Create an outward burst for cancel animation."""
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
            # Warm hues for cancel burst
            particle.color_hue = random.uniform(0, 40)
            self.cancel_particles.append(particle)

        self._cancel_initialized = True
        self._last_cancel_progress = 0.0
        self._last_cancel_update = time.time()

    def _cancel_dt(self) -> float:
        """Compute time delta for cancel animation."""
        now = time.time()
        if self._last_cancel_update is None:
            self._last_cancel_update = now
            return 1 / 30

        dt = now - self._last_cancel_update
        self._last_cancel_update = now
        return max(0.0, min(0.05, dt))

    def _update_cancel_particles(self, dt: float):
        """Update cancel burst particles."""
        alive = []
        for particle in self.cancel_particles:
            # Small random wobble to keep the burst organic
            particle.vx += random.uniform(-25, 25) * dt
            particle.vy += random.uniform(-25, 25) * dt

            if particle.update(dt, gravity=0, damping=0.92):
                alive.append(particle)

        self.cancel_particles = alive

    def _draw_cancel_particles(self, painter: QPainter, progress: float):
        """Render cancel burst particles."""
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
