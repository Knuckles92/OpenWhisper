"""Word-wrapped label that reports the height its text actually occupies."""
from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QLabel


class WrappedLabel(QLabel):
    """Word-wrapped label whose size hints match the text at its own width.

    ``QLabel`` derives the size hint for wrapped text from a heuristic width, so
    its reported height rarely matches the height the text needs once a layout
    has assigned a width. Nested cards pay for that: the difference is taken out
    of a sibling widget, which is how the Meeting Mode step rows ended up
    overlapping. Reporting the height for the current width keeps the enclosing
    layout's minimum honest instead.
    """

    def __init__(self, text: str = "", parent=None):
        """Create a word-wrapped label.

        Args:
            text: Initial label text.
            parent: Optional parent widget.
        """
        super().__init__(text, parent)
        self.setWordWrap(True)

    def hasHeightForWidth(self) -> bool:
        """Report a width-independent height so layouts use the size hints.

        Qt's height-for-width path replaces a nested card's minimum with an
        estimate taken at the wrong width, which is what compressed sibling
        widgets. The size hints below already carry the wrapped height.
        """
        return False

    def sizeHint(self) -> QSize:
        """Return the preferred size with the height the wrapped text needs."""
        hint = super().sizeHint()
        width = self.width()
        if width <= 0:
            return hint
        return QSize(hint.width(), self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        """Return the minimum size with the height the wrapped text needs."""
        hint = super().minimumSizeHint()
        width = self.width()
        if width <= 0:
            return hint
        return QSize(hint.width(), self.heightForWidth(width))

    def resizeEvent(self, event):
        """Re-report geometry when the wrap width changes.

        Args:
            event: Resize event carrying the old and new size.
        """
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()
