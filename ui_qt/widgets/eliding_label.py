"""Single-line label whose width floor ignores how long its text is.

A plain ``QLabel`` reports the full width of its text as its minimum, so one
long secondary line inside a row or card raises the minimum width of every
ancestor up to the window. This caps that floor, elides to whatever width it is
given, and keeps the full text available as a tooltip.
"""
from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QLabel


class ElidingLabel(QLabel):
    #: Room for a few characters plus the ellipsis.
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

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(
            metrics.horizontalAdvance(self._full_text),
            super().sizeHint().height(),
        )

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), self.MINIMUM_WIDTH), hint.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        metrics = self.fontMetrics()
        available = self.width() or metrics.horizontalAdvance(self._full_text)
        # QLabel.setText short-circuits on equal text, so this cannot recurse
        # through the resize it may trigger.
        QLabel.setText(
            self, metrics.elidedText(self._full_text, self._mode, available)
        )
