"""A click-through screenshot card that flies into the walkthrough column."""

from __future__ import annotations

import math
import sys

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

from ui_qt.snap import keep_out_of_screenshots, macos
from ui_qt.utils.palette import token_color

FLIGHT_S = 0.68


class ScreenshotFlight(QWidget):
    """One short flight, driven by the panel's existing frame timer.

    Only a thumbnail is retained. The window takes neither input nor focus;
    the panel lands it before another capture can see it.
    """

    def __init__(self, panel: QWidget) -> None:
        super().__init__(panel, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
            macos.make_nonactivating(self)
        self.number = 0
        self.image = QImage()
        self.source = QRectF()
        self.target = QRectF()
        self.progress = 0.0

    def start(self, number: int, image: QImage, source: QRectF, target: QRectF) -> None:
        self.number = number
        self.image = image
        self.source = QRectF(source)
        self.progress = 0.0
        self.advance(0.0, target)
        keep_out_of_screenshots(self, True)
        self.show()
        self.raise_()

    def land(self) -> None:
        self.hide()
        self.number = 0
        self.image = QImage()

    def advance(self, progress: float, target: QRectF) -> None:
        self.progress = min(1.0, max(0.0, progress))
        self.target = QRectF(target)
        # Include the lifted arc, rotation and shadow, even across monitors.
        bounds = self.source.united(target).adjusted(-64, -128, 64, 64)
        self.setGeometry(bounds.toAlignedRect())
        self.update()

    def card_rect(self) -> QRectF:
        t = self.progress
        eased = 1.0 - (1.0 - t) ** 3
        start, end = self.source.center(), self.target.center()
        centre = start + (end - start) * eased
        centre.setY(centre.y() - 64 * math.sin(math.pi * eased))
        width = self.source.width() + (self.target.width() - self.source.width()) * eased
        height = self.source.height() + (self.target.height() - self.source.height()) * eased
        return QRectF(centre.x() - width / 2, centre.y() - height / 2, width, height)

    def paintEvent(self, event) -> None:
        if self.image.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing
                               | QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.card_rect().translated(-QPointF(self.pos()))
        lift = math.sin(math.pi * self.progress)
        painter.translate(rect.center())
        painter.rotate(-3.5 * lift)
        painter.translate(-rect.center())
        painter.setPen(Qt.PenStyle.NoPen)
        for spread in (18, 12, 6):
            painter.setBrush(QColor(0, 0, 0, int(10 + 12 * lift)))
            painter.drawRoundedRect(rect.adjusted(-spread / 2, -spread / 2 + 5,
                                                 spread / 2, spread / 2 + 5), 12, 12)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 6 + 6 * lift, 6 + 6 * lift)
        painter.save()
        painter.setClipPath(clip)
        painter.drawImage(rect, self.image)
        painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(token_color("accent", int(190 * lift)), 1.5))
        painter.drawPath(clip)
        painter.end()
