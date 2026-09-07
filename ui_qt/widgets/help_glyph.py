"""A painted help glyph: a circled ``i`` or ``?`` drawn as geometry.

Font glyphs such as ``ⓘ`` fall back to whichever face carries them and hint to
the pixel grid at caption sizes, which is what made the earlier icons look
ragged. Drawing the ring and the mark with the painter keeps them crisp at any
size and device pixel ratio, and lets the colour follow the palette.
"""
from enum import Enum

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QToolButton

from ui_qt.utils.palette import token_color


class HelpSymbol(Enum):
    INFO = "i"
    QUESTION = "?"


def glyph_diameter(metrics: QFontMetrics, factor: float = 1.6, floor: int = 12) -> int:
    """Size a glyph to sit beside text set in ``metrics``.

    The cap height tracks the visual weight of the neighbouring text better
    than the line height, which includes ascender and descender room the
    glyph should not fill.
    """
    return max(floor, round(metrics.capHeight() * factor))


def paint_help_glyph(
    painter: QPainter,
    rect: QRectF,
    color: QColor,
    symbol: HelpSymbol = HelpSymbol.INFO,
    fill: QColor | None = None,
) -> None:
    """Draw the glyph inside ``rect`` (a square) in ``color``.

    Proportions are fractions of the diameter so the mark keeps its shape as
    the glyph scales with the font.
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    d = rect.width()
    ring_width = max(1.1, d * 0.09)
    ring = rect.adjusted(ring_width / 2, ring_width / 2, -ring_width / 2, -ring_width / 2)
    painter.setPen(QPen(color, ring_width))
    painter.setBrush(fill if fill is not None else Qt.BrushStyle.NoBrush)
    painter.drawEllipse(ring)

    def at(x: float, y: float) -> QPointF:
        return QPointF(rect.left() + x * d, rect.top() + y * d)

    stroke = max(1.3, d * 0.12)
    pen = QPen(color, stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if symbol is HelpSymbol.INFO:
        painter.drawLine(at(0.5, 0.46), at(0.5, 0.70))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(at(0.5, 0.30), d * 0.075, d * 0.075)
    else:
        hook = QPainterPath()
        radius = 0.15 * d
        centre = at(0.5, 0.41)
        bowl = QRectF(centre.x() - radius, centre.y() - radius, 2 * radius, 2 * radius)
        hook.arcMoveTo(bowl, 165)
        hook.arcTo(bowl, 165, -210)
        hook.quadTo(at(0.5, 0.58), at(0.5, 0.66))
        painter.drawPath(hook)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(at(0.5, 0.79), d * 0.075, d * 0.075)
    painter.restore()


def glyph_colors(active: bool) -> tuple[QColor, QColor | None]:
    """The (stroke, fill) pair for a glyph at rest or under the pointer/focus."""
    if active:
        return token_color("accent"), token_color("accent", 36)
    return token_color("text-secondary"), None


class HelpGlyphButton(QToolButton):
    """A standalone circled ``?`` (or ``i``) that opens help on click.

    ``text()`` still reports the symbol so accessibility and tests read it,
    but nothing is rendered through the style: the button paints only the
    glyph, which is why it needs no QSS.
    """

    def __init__(self, symbol: HelpSymbol = HelpSymbol.QUESTION, parent=None):
        super().__init__(parent)
        self._symbol = symbol
        self.setText(symbol.value)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setStyleSheet("QToolButton { background: transparent; border: none; padding: 0; }")
        d = glyph_diameter(self.fontMetrics(), factor=1.8, floor=16)
        self.setFixedSize(d, d)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        color, fill = glyph_colors(self.underMouse() or self.hasFocus() or self.isDown())
        paint_help_glyph(painter, QRectF(self.rect()), color, self._symbol, fill)
