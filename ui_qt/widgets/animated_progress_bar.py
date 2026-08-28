"""Rounded progress bar with an eased fill and a looping indeterminate sweep.

``QProgressBar`` under the app stylesheet draws a flat rectangle that snaps
between values; an update download is the one place the app asks the user to
wait on it, so the bar animates: the fill eases toward each reported value and
a highlight travels across it, and a gradient pill sweeps the track while the
work is indeterminate.
"""
from __future__ import annotations

from typing import Final

from PyQt6.QtCore import QRectF, QTimer, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PyQt6.QtWidgets import QSizePolicy, QWidget

_TRACK: Final[QColor] = QColor(255, 255, 255, 22)
_FILL_START: Final[QColor] = QColor("#0a84ff")
_FILL_END: Final[QColor] = QColor("#64d2ff")
_SHEEN: Final[QColor] = QColor(255, 255, 255, 70)

_TICK_MS: Final[int] = 16
_SWEEP_MS: Final[float] = 1500.0
_SHEEN_MS: Final[float] = 1900.0
_EASE: Final[float] = 0.18


class AnimatedProgressBar(QWidget):
    """Determinate or indeterminate progress strip, animated while visible."""

    def __init__(self, parent=None, bar_height: int = 8):
        super().__init__(parent)
        self.setObjectName("animatedProgressBar")
        self.setFixedHeight(bar_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._indeterminate = True
        self._target = 0.0
        self._display = 0.0
        self._phase = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def is_indeterminate(self) -> bool:
        """Whether the bar is sweeping instead of showing a value."""
        return self._indeterminate

    @property
    def fraction(self) -> float:
        """Latest requested fill, 0.0 to 1.0, ignoring the easing in flight."""
        return self._target

    def set_indeterminate(self) -> None:
        """Sweep the track for work whose size is not known yet."""
        if not self._indeterminate:
            self._indeterminate = True
            self._phase = 0.0
        self.update()

    def set_fraction(self, fraction: float, animate: bool = True) -> None:
        """Fill to ``fraction`` (0.0-1.0), easing toward it unless told not to."""
        value = max(0.0, min(1.0, float(fraction)))
        if self._indeterminate:
            self._indeterminate = False
            self._phase = 0.0
            self._display = 0.0 if animate else value
        self._target = value
        if not animate:
            self._display = value
        self.update()

    def reset(self) -> None:
        """Return to an empty determinate bar."""
        self._indeterminate = False
        self._target = 0.0
        self._display = 0.0
        self._phase = 0.0
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def _tick(self) -> None:
        period = _SWEEP_MS if self._indeterminate else _SHEEN_MS
        self._phase = (self._phase + _TICK_MS / period) % 1.0
        if not self._indeterminate:
            delta = self._target - self._display
            if abs(delta) < 0.001:
                self._display = self._target
            else:
                self._display += delta * _EASE
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        rect = QRectF(self.rect())
        if rect.width() <= 0 or rect.height() <= 0:
            return
        radius = rect.height() / 2.0

        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, _TRACK)
        painter.setClipPath(track)

        if self._indeterminate:
            self._paint_sweep(painter, rect)
        else:
            self._paint_fill(painter, rect, radius)

    def _paint_sweep(self, painter: QPainter, rect: QRectF) -> None:
        span = max(rect.width() * 0.34, 56.0)
        eased = self._phase * self._phase * (3.0 - 2.0 * self._phase)
        left = -span + (rect.width() + span) * eased

        gradient = QLinearGradient(left, 0.0, left + span, 0.0)
        gradient.setColorAt(0.0, QColor(10, 132, 255, 0))
        gradient.setColorAt(0.45, _FILL_START)
        gradient.setColorAt(0.75, _FILL_END)
        gradient.setColorAt(1.0, QColor(100, 210, 250, 0))
        painter.fillRect(QRectF(left, rect.top(), span, rect.height()), gradient)

    def _paint_fill(self, painter: QPainter, rect: QRectF, radius: float) -> None:
        if self._display <= 0.0:
            return
        width = max(rect.width() * self._display, radius * 2.0)
        filled = QRectF(rect.left(), rect.top(), width, rect.height())

        gradient = QLinearGradient(rect.left(), 0.0, rect.right(), 0.0)
        gradient.setColorAt(0.0, _FILL_START)
        gradient.setColorAt(1.0, _FILL_END)

        path = QPainterPath()
        path.addRoundedRect(filled, radius, radius)
        painter.fillPath(path, gradient)

        if self._display < 0.999:
            painter.save()
            painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
            band = max(width * 0.28, 40.0)
            left = -band + (width + band) * self._phase
            sheen = QLinearGradient(left, 0.0, left + band, 0.0)
            sheen.setColorAt(0.0, QColor(255, 255, 255, 0))
            sheen.setColorAt(0.5, _SHEEN)
            sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.fillRect(
                QRectF(left, rect.top(), band, rect.height()), sheen
            )
            painter.restore()
