"""A small "New" or "?" chip that explains a feature.

It sits on a button's right side, or on its own in a layout beside the
feature's controls (pass ``button=None`` and a parent).
"""
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QPushButton, QToolTip, QWidget

from ui_qt.utils.palette import token_color


class FeatureBadge(QWidget):
    """Hover or click the chip for what its button does.

    The button's own hover keeps showing its shortcut pill above it, so the
    explanation lives here instead of in the button's tooltip. A click on
    the chip shows the explanation and never reaches the button.

    On a button it is frosted glass over the button's fill: a tint of the
    label colour, not a sticker of its own colour, so it reads as part of the
    button. On its own it is an accent-tinted pill with accent text.
    """

    NEW = "New"
    HELP = "?"
    _HEIGHT = 18
    _RIGHT_INSET = 12  # from the button's right edge, clear of its rounding
    _PAD = 7  # inside the "New" chip, left and right
    _SPARKLE = 10  # the star before "NEW"
    _GAP = 4  # between the star and the word
    _CLEARANCE = 8  # kept between the chip and the button's centred label

    def __init__(self, button: QPushButton | None, explanation: str,
                 parent: QWidget | None = None):
        super().__init__(button if button is not None else parent)
        self.setObjectName("featureBadge")
        self._button = button
        self._text = self.NEW
        self._hovered = False
        # In a narrow button "New" shrinks to its star, clear of the label.
        self._compact = False
        self._placed_for: tuple = ()
        self._word_font = QFont(self.font())
        self._word_font.setPixelSize(9)
        self._word_font.setWeight(QFont.Weight.Bold)
        self._word_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
        self._mark_font = QFont(self.font())
        self._mark_font.setPixelSize(11)
        self._mark_font.setWeight(QFont.Weight.Bold)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.set_explanation(explanation)
        if button is not None:
            # Follows the button's size and its active/inactive look.
            button.installEventFilter(self)
        self._place()

    def set_explanation(self, explanation: str) -> None:
        # Rich text, so the tooltip wraps instead of running off one line.
        self.setToolTip(f"<p style='white-space:normal'>{explanation}</p>")
        self.setAccessibleDescription(explanation)

    def set_new(self, new: bool) -> None:
        """Say "New" for a feature not tried yet, else "?"."""
        self._text = self.NEW if new else self.HELP
        self.setAccessibleName(f"{'New: ' if new else ''}what this button does")
        self._place()
        self.update()

    def text(self) -> str:
        return self._text

    def is_compact(self) -> bool:
        return self._compact

    @classmethod
    def room_beside_label(cls) -> int:
        """Width a button needs beyond its centred label for the chip, at its
        most compact, to clear that label (the same margin on both sides)."""
        return 2 * (cls._RIGHT_INSET + cls._CLEARANCE + cls._HEIGHT)

    def sizeHint(self) -> QSize:
        return self._size(self._compact)

    def _size(self, compact: bool) -> QSize:
        if self._text == self.HELP or compact:
            return QSize(self._HEIGHT, self._HEIGHT)
        # Letter spacing trails the last letter too; that space is not ink.
        word = (QFontMetrics(self._word_font).horizontalAdvance(self._text.upper())
                - round(self._word_font.letterSpacing()))
        return QSize(self._PAD * 2 + self._SPARKLE + self._GAP + word, self._HEIGHT)

    def _place(self) -> None:
        button = self._button
        if button is None:
            # Standalone: the layout places it; only its size follows the word.
            self.setFixedSize(self.sizeHint())
            self.update()
            return
        key = (button.width(), button.height(), button.text(), self._text)
        if key == self._placed_for:
            return
        self._placed_for = key
        label = button.fontMetrics().horizontalAdvance(button.text())
        room = (button.width() - label) // 2 - self._RIGHT_INSET - self._CLEARANCE
        self._compact = self._size(False).width() > room
        size = self.sizeHint()
        self.resize(size)
        x = button.width() - size.width() - self._RIGHT_INSET
        self.move(x, (button.height() - size.height()) // 2)
        self.raise_()
        self.update()

    def event(self, event):
        if event.type() in (QEvent.Type.HoverEnter, QEvent.Type.HoverLeave):
            self._hovered = event.type() == QEvent.Type.HoverEnter
            self.update()
        return super().event(event)

    def mousePressEvent(self, event):
        QToolTip.showText(self.mapToGlobal(QPoint(0, self.height() + 4)), self.toolTip(), self)
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def eventFilter(self, obj, event):
        if obj is self._button:
            # A new label ("Finish Walkthrough") repaints the button without
            # resizing it; _place returns at once when nothing changed.
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Paint):
                self._place()
            elif event.type() == QEvent.Type.DynamicPropertyChange:
                self.update()  # "inactive" changed the button's look
        return False

    def _colors(self):
        """``(ink, fill, edge)`` for the button's current look."""
        if self._button is None:
            ink = token_color("accent")
            fill = token_color("accent", 70 if self._hovered else 38)
            return ink, fill, token_color("accent", 140 if self._hovered else 90)
        if self._button.property("inactive"):
            # The button is flat and grey while a dictation runs.
            ink = token_color("text-muted")
            return ink, token_color("text-muted", 28), token_color("text-muted", 70)
        ink = token_color("on-accent")
        fill = token_color("on-accent", 92 if self._hovered else 46)
        return ink, fill, token_color("on-accent", 150 if self._hovered else 80)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink, fill, edge = self._colors()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2.0
        painter.setPen(QPen(edge, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)
        if self._text == self.HELP:
            painter.setFont(self._mark_font)
            painter.setPen(ink)
            # Half a pixel down: a question mark's ink sits high in its box.
            painter.drawText(rect.translated(0, 0.5), Qt.AlignmentFlag.AlignCenter, self.HELP)
            return
        star_x = rect.center().x() if self._compact else self._PAD + self._SPARKLE / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        painter.drawPath(_sparkle(QPointF(star_x, rect.center().y()), self._SPARKLE / 2))
        if self._compact:
            return
        painter.setFont(self._word_font)
        painter.setPen(ink)
        word_left = self._PAD + self._SPARKLE + self._GAP
        word_rect = QRectF(word_left, 0, self.width() - word_left - self._PAD + 2, self.height())
        painter.drawText(word_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         self._text.upper())


def _sparkle(center: QPointF, radius: float) -> QPainterPath:
    """A four-point sparkle: points on the axes joined by inward curves.

    Straight sides with a narrow waist read as a plus sign at this size.
    """
    pull = radius * 0.08  # how far each curve bends toward the centre
    points = [(0, -radius), (radius, 0), (0, radius), (-radius, 0)]
    path = QPainterPath(QPointF(center.x(), center.y() - radius))
    for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
        bend = QPointF(center.x() + (x0 + x1) * pull / radius,
                       center.y() + (y0 + y1) * pull / radius)
        path.quadTo(bend, QPointF(center.x() + x1, center.y() + y1))
    path.closeSubpath()
    return path
