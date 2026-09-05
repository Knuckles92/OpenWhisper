"""Single-line label whose width floor ignores how long its text is.

A plain ``QLabel`` reports the full width of its text as its minimum, so one
long secondary line inside a row or card raises the minimum width of every
ancestor up to the window. This caps that floor, elides to whatever width it is
given, and keeps the full text available as a tooltip.
"""
from math import ceil

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QFontMetricsF
from PyQt6.QtWidgets import QLabel


class ElidingLabel(QLabel):
    #: Room for a few characters plus the ellipsis, excluding any padding or
    #: border the stylesheet draws around the text.
    MINIMUM_WIDTH = 56

    def __init__(
        self,
        text: str = "",
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
        parent=None,
    ):
        super().__init__(parent)
        self._full_text = ""
        self._mode = mode
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._apply_elide()

    def text(self) -> str:
        """Return the full text, not the elided form being displayed."""
        return self._full_text

    def _chrome_width(self) -> int:
        """Horizontal space QLabel keeps around the text, mirroring its sizeHint.

        A stylesheet ``padding`` or ``border`` lands in the contents margins and
        also gives the label a frame. QLabel then applies an implicit indent of
        one "x" advance on the aligned side, so a framed label asks for more
        than text plus margins. Reproducing that rule here keeps the size hint
        and the elide width consistent with what QLabel actually paints.
        """
        margins = self.contentsMargins()
        chrome = margins.left() + margins.right() + 2 * self.margin()
        indent = self.indent()
        if indent < 0 and self.frameWidth():
            indent = self.fontMetrics().horizontalAdvance("x") - 2 * self.margin()
        horizontal = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignRight
        if indent > 0 and self.alignment() & horizontal:
            chrome += indent
        return chrome

    def sizeHint(self) -> QSize:
        # elidedText compares fractional advances; integer metrics can round
        # down and elide text even when the layout grants our entire hint.
        metrics = QFontMetricsF(self.font())
        return QSize(
            ceil(metrics.horizontalAdvance(self._full_text)) + self._chrome_width(),
            super().sizeHint().height(),
        )

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        floor = self.MINIMUM_WIDTH + self._chrome_width()
        return QSize(min(hint.width(), floor), hint.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        metrics = self.fontMetrics()
        # Only the area inside the padding, border, and indent is painted.
        available = (
            self.width() - self._chrome_width()
            if self.width()
            else metrics.horizontalAdvance(self._full_text)
        )
        # QLabel.setText short-circuits on equal text, so this cannot recurse
        # through the resize it may trigger.
        QLabel.setText(
            self, metrics.elidedText(self._full_text, self._mode, available)
        )
