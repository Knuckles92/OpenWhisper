"""The Remote card's entry point to the paired host's model library."""

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QPushButton, QSizePolicy

from ui_qt.utils.palette import token_color

#: The glyph's box; the stylesheet's left padding leaves room for it.
GLYPH_SIZE = 14


def _download_path() -> QPainterPath:
    """Tabler's download icon, on its 24-unit grid."""
    path = QPainterPath(QPointF(4, 17))
    path.lineTo(4, 19)
    path.arcTo(QRectF(4, 17, 4, 4), 180, 90)
    path.lineTo(18, 21)
    path.arcTo(QRectF(16, 17, 4, 4), 270, 90)
    path.lineTo(20, 17)
    path.moveTo(7, 11)
    path.lineTo(12, 16)
    path.lineTo(17, 11)
    path.moveTo(12, 4)
    path.lineTo(12, 16)
    return path


class RemoteModelNotice(QPushButton):
    """"Manage host models", as a link on the engine card's footer line.

    It rides that line so the Remote card stays as tall as every other
    backend's. When the host reports something it could install, a dot on
    the glyph says so and the tooltip names it.
    """

    def __init__(self, parent=None):
        super().__init__("Manage host models", parent)
        self.setObjectName("remoteModelNotice")
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # A click shouldn't leave the link underlined; Tab still reaches it.
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        # Takes the line's height rather than setting it: a push button's
        # native minimum is taller than the footer's text.
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Ignored)
        self._glyph = _download_path()
        self.available: list[str] = []
        self.set_available([])

    def set_available(self, names: list[str]) -> None:
        """What the host could install, by label; empty when nothing."""
        self.available = list(names)
        tip = "Browse models, manage downloads, and set up CPU or GPU on the paired host."
        if names:
            tip = f"Available to install on the paired host: {', '.join(names)}. {tip}"
        self.setToolTip(tip)
        self.setAccessibleDescription(tip)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(0, (self.height() - GLYPH_SIZE) / 2)
        painter.scale(GLYPH_SIZE / 24, GLYPH_SIZE / 24)
        # The text's colour, hover included, so glyph and words read as one link.
        hovered = self.underMouse() and not self.isDown()
        pen = QPen(token_color("accent-soft" if hovered else "accent"), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(self._glyph)
        if self.available:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(token_color("warning"))
            painter.drawEllipse(QPointF(20.5, 4), 3.5, 3.5)
