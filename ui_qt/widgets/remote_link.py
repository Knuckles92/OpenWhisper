"""The Remote engine's link, drawn as this computer, a wire, and the host.

Every motion here answers something that actually happened on the
connection, the way the level meter answers the microphone:

* a soft pulse runs home along the wire when a keepalive ping comes back
  (RemoteConnection pings on connecting and then every 20 s);
* packets run out while a request waits on the host, and one runs home
  when its reply lands;
* dots search along the wire while a connection is being made;
* the first time a host answers, the wire draws itself across and the
  host's light comes on.

Offline or unpaired, the wire is broken and nothing moves. The same glyph,
mirrored, is the host's "serving" chip: there the host is on the left and
the paired computer's requests arrive from the right.
"""
from __future__ import annotations

from math import sin, pi

from PyQt6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from ui_qt.utils.palette import token_color

GLYPH_WIDTH = 44
GLYPH_HEIGHT = 16

_WIRE_START = 16.0
_WIRE_END = 30.0
_WIRE_Y = 8.0

#: Milliseconds a pulse or packet takes to cross the wire.
_HEARTBEAT_MS = 900
_REPLY_MS = 420
_SEND_PERIOD_MS = 760
_SEARCH_PERIOD_MS = 1100
_DRAW_MS = 520
_LIGHT_MS = 460


def _ease(x: float) -> float:
    return 2 * x * x if x < 0.5 else 1 - (-2 * x + 2) ** 2 / 2


class RemoteLinkGlyph(QWidget):
    """Link state for one paired computer; clicking asks to reconnect."""

    clicked = pyqtSignal()

    def __init__(self, parent=None, *, mirrored: bool = False):
        super().__init__(parent)
        self.setObjectName("remoteLinkGlyph")
        self.setFixedSize(GLYPH_WIDTH, GLYPH_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._mirrored = mirrored
        self._state = "unpaired"
        self._busy = False
        self._replies = 0
        self._beat = 0
        self._greeted = False
        #: (start ms, duration ms, kind) for pulses and packets heading home.
        self._flights: list[tuple[int, int, str]] = []
        self._drawn_at: int | None = None
        self._busy_since = 0
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    # ---- state ----

    @property
    def state(self) -> str:
        return self._state

    def set_link(self, state: str, *, busy: bool = False, replies: int = 0,
                 beat: int = 0) -> None:
        now = self._clock.elapsed()
        connected = state == "connected"
        if connected and not self._greeted:
            # The first answer this glyph has seen: draw the wire across.
            self._greeted = True
            self._drawn_at = now
        elif connected and self._state == "connected":
            if replies > self._replies:
                self._flights.append((now, _REPLY_MS, "reply"))
            if beat > self._beat and not busy:
                self._flights.append((now, _HEARTBEAT_MS, "beat"))
        if busy and not self._busy:
            self._busy_since = now
        if not connected:
            self._flights.clear()
            self._drawn_at = None
        self._state = state
        self._busy = busy and connected
        self._replies = replies
        self._beat = beat
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if state == "offline" else Qt.CursorShape.ArrowCursor
        )
        self._sync_animation()
        self.update()

    def _animating(self) -> bool:
        return (self._state == "connecting" or self._busy or bool(self._flights)
                or self._drawn_at is not None)

    def _sync_animation(self) -> None:
        if self._animating() and self.isVisible():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _tick(self) -> None:
        now = self._clock.elapsed()
        self._flights = [f for f in self._flights if now - f[0] < f[1]]
        if self._drawn_at is not None and now - self._drawn_at >= _DRAW_MS + _LIGHT_MS:
            self._drawn_at = None
        self._sync_animation()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_animation()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    # ---- painting ----

    def _wire_color(self) -> QColor:
        if self._state == "connected":
            return token_color("accent" if self._busy else "success")
        if self._state == "offline":
            return token_color("warning")
        return token_color("text-muted")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._mirrored:
            painter.translate(self.width(), 0)
            painter.scale(-1, 1)
        now = self._clock.elapsed()
        self._paint_devices(painter)
        self._paint_wire(painter, now)
        self._paint_light(painter, now)
        self._paint_motion(painter, now)

    def _paint_devices(self, painter: QPainter) -> None:
        pen = QPen(token_color("text-secondary"), 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # This computer: a laptop.
        painter.drawRoundedRect(QRectF(2.5, 3.5, 10, 7), 1.5, 1.5)
        painter.drawLine(QPointF(0.8, 12.8), QPointF(14.2, 12.8))
        # The host: two rack units.
        painter.drawRoundedRect(QRectF(32.5, 2.2, 10, 5.2), 1.3, 1.3)
        painter.drawRoundedRect(QRectF(32.5, 8.6, 10, 5.2), 1.3, 1.3)

    def _paint_wire(self, painter: QPainter, now: int) -> None:
        pen = QPen(self._wire_color(), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self._state in ("offline", "unpaired"):
            painter.drawLine(QPointF(_WIRE_START, _WIRE_Y), QPointF(20.5, _WIRE_Y))
            painter.drawLine(QPointF(25.5, _WIRE_Y), QPointF(_WIRE_END, _WIRE_Y))
            return
        if self._state == "connecting":
            pen.setColor(token_color("text-muted", 150))
            painter.setPen(pen)
        end = _WIRE_END
        if self._drawn_at is not None:
            end = _WIRE_START + (_WIRE_END - _WIRE_START) * _ease(min(1.0, (now - self._drawn_at) / _DRAW_MS))
        painter.drawLine(QPointF(_WIRE_START, _WIRE_Y), QPointF(end, _WIRE_Y))

    def _paint_light(self, painter: QPainter, now: int) -> None:
        center = QPointF(39.8, 4.8)
        painter.setPen(Qt.PenStyle.NoPen)
        if self._state == "connected":
            lit = True
            if self._drawn_at is not None:
                elapsed = now - self._drawn_at - _DRAW_MS
                lit = elapsed >= 0
                if 0 <= elapsed < _LIGHT_MS:
                    # The host's light comes on with a ring, once.
                    progress = elapsed / _LIGHT_MS
                    ring = QPen(token_color("success", int(200 * (1 - progress))), 1.2)
                    painter.setPen(ring)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawEllipse(center, 1.4 + 3.2 * progress, 1.4 + 3.2 * progress)
                    painter.setPen(Qt.PenStyle.NoPen)
            if lit:
                painter.setBrush(token_color("accent-cyan" if self._busy else "success"))
                painter.drawEllipse(center, 1.3, 1.3)
        elif self._state == "offline":
            painter.setBrush(token_color("warning"))
            painter.drawEllipse(center, 1.3, 1.3)

    def _paint_motion(self, painter: QPainter, now: int) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        span = _WIRE_END - _WIRE_START
        if self._state == "connecting":
            for index in range(3):
                phase = ((now / _SEARCH_PERIOD_MS) + index / 3) % 1.0
                painter.setBrush(token_color("accent", int(230 * sin(phase * pi))))
                painter.drawEllipse(QPointF(_WIRE_START + span * phase, _WIRE_Y), 1.5, 1.5)
            return
        if self._busy:
            # Audio on its way out.
            elapsed = now - self._busy_since
            for index in range(2):
                phase = ((elapsed / _SEND_PERIOD_MS) + index / 2) % 1.0
                painter.setBrush(token_color("accent-cyan", int(255 * sin(phase * pi))))
                painter.drawEllipse(QPointF(_WIRE_START + span * _ease(phase), _WIRE_Y), 1.8, 1.8)
        for start, duration, kind in self._flights:
            progress = min(1.0, (now - start) / duration)
            x = _WIRE_END - span * _ease(progress)
            fade = sin(progress * pi)
            if kind == "reply":
                painter.setBrush(token_color("success", int(255 * fade)))
                painter.drawEllipse(QPointF(x, _WIRE_Y), 1.9, 1.9)
            else:
                # A heartbeat is softer than a reply: a wider, fainter glow.
                painter.setBrush(token_color("success", int(110 * fade)))
                painter.drawEllipse(QPointF(x, _WIRE_Y), 2.6, 2.6)
                painter.setBrush(token_color("success", int(220 * fade)))
                painter.drawEllipse(QPointF(x, _WIRE_Y), 1.2, 1.2)
