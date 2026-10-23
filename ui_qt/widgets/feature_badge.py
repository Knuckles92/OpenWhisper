"""A small "New" or "?" pill in a button's top-right corner that explains it."""
from PyQt6.QtCore import QEvent, QPoint, QRectF, QSize, Qt
from PyQt6.QtGui import QFont, QPainter, QPen
from PyQt6.QtWidgets import QPushButton, QToolTip, QWidget

from ui_qt.utils.palette import token_color


class FeatureBadge(QWidget):
    """Hover or click the pill for what its button does.

    The button's own hover keeps showing its shortcut pill above it, so the
    explanation lives here instead of in the button's tooltip. A click on
    the badge shows the explanation and never reaches the button.
    """

    NEW = "New"
    HELP = "?"
    _INSET = 5  # from the button's top and right edges
    _HEIGHT = 16

    def __init__(self, button: QPushButton, explanation: str):
        super().__init__(button)
        self.setObjectName("featureBadge")
        self._button = button
        self._text = self.NEW
        font = QFont(self.font())
        font.setPixelSize(10)
        font.setWeight(QFont.Weight.Bold)
        self.setFont(font)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.set_explanation(explanation)
        # Follows the corner as the button resizes.
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

    def sizeHint(self) -> QSize:
        if self._text == self.HELP:
            return QSize(self._HEIGHT, self._HEIGHT)
        width = self.fontMetrics().horizontalAdvance(self._text.upper()) + 12
        return QSize(width, self._HEIGHT)

    def _place(self) -> None:
        size = self.sizeHint()
        self.resize(size)
        self.move(self._button.width() - size.width() - self._INSET, self._INSET)
        self.raise_()

    def mousePressEvent(self, event):
        QToolTip.showText(self.mapToGlobal(QPoint(0, self.height())), self.toolTip(), self)
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2.0
        # White on the accent fill, and still a pill with an edge on the
        # flat look the button has while another recording runs.
        painter.setPen(QPen(token_color("accent-border"), 1.0))
        painter.setBrush(token_color("on-accent"))
        painter.drawRoundedRect(rect, radius, radius)
        painter.setPen(token_color("accent-pressed"))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text.upper())

    def eventFilter(self, obj, event):
        if obj is self._button and event.type() == QEvent.Type.Resize:
            self._place()
        return False
