import re

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import QApplication, QLabel

_BREAK = chr(0x200B)  # zero-width space: a line break opportunity that draws nothing
# A run with no space, hyphen, or slash to wrap at: hashes, long names, path segments.
_UNBREAKABLE_RUN = re.compile("[^\\s" + _BREAK + "\\-/\\\\]{16,}")
_CHUNK = 8


def _with_break_points(text: str) -> str:
    """Let a long unbroken value wrap: after each backslash, and every few characters."""
    text = text.replace("\\", "\\" + _BREAK)
    return _UNBREAKABLE_RUN.sub(
        lambda run: _BREAK.join(
            run.group()[start:start + _CHUNK] for start in range(0, len(run.group()), _CHUNK)
        ),
        text,
    )


class WrappedLabel(QLabel):
    """Word-wrapped label whose size hints match the text at its own width.

    ``QLabel`` derives the size hint for wrapped text from a heuristic width, so
    its reported height rarely matches the height the text needs once a layout
    has assigned a width. Nested cards pay for that: the difference is taken out
    of a sibling widget, which is how the Meeting Mode step rows ended up
    overlapping. Reporting the height for the current width keeps the enclosing
    layout's minimum honest instead.

    ``break_long_words`` is for values that are not prose (a commit hash, a model
    folder path). ``QLabel`` cannot wrap a word that is wider than the label: it
    asks its layout for that whole width and is clipped when it does not get it.
    With the option on, such runs get invisible break points, while ``text()``,
    ``selectedText()``, and anything copied still return the original text.
    """

    def __init__(self, text: str = "", parent=None, *, break_long_words: bool = False):
        super().__init__(parent)
        self._break_long_words = break_long_words
        self._plain = ""
        self.setWordWrap(True)
        self.setText(text)

    def setText(self, text: str) -> None:
        """Show ``text``; with ``break_long_words``, long unbroken runs may wrap."""
        self._plain = text
        super().setText(_with_break_points(text) if self._break_long_words else text)

    def text(self) -> str:
        """Return the text as it was set, without any break points."""
        return self._plain if self._break_long_words else super().text()

    def selectedText(self) -> str:
        """Return the selection without any break points."""
        return super().selectedText().replace(_BREAK, "")

    def keyPressEvent(self, event):
        """Copy the selection as the original text, not with its break points."""
        super().keyPressEvent(event)
        if self._break_long_words and event.matches(QKeySequence.StandardKey.Copy):
            self._clean_clipboard()

    def contextMenuEvent(self, event):
        """The standard menu's Copy goes through the same clean-up."""
        super().contextMenuEvent(event)
        if self._break_long_words:
            self._clean_clipboard()

    def _clean_clipboard(self) -> None:
        clipboard = QApplication.clipboard()
        copied = clipboard.text()
        clean = copied.replace(_BREAK, "")
        # Only ever repair a copy of this label's own text, never another app's clipboard.
        if clean != copied and clean in self._plain:
            clipboard.setText(clean)

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
        """Re-report geometry when the wrap width changes."""
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()
