"""A layout that wraps its items like words in a paragraph."""
from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtWidgets import QLayout


class FlowLayout(QLayout):
    """Lays children out left to right and wraps them, each at its own width.

    Items on one line are centred on it, so a chip sits level with the taller
    buttons beside it.
    """

    def __init__(self, parent=None, *, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, move=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        return size

    def line_width(self) -> int:
        """The width that shows every item on one line."""
        widths = [item.sizeHint().width() for item in self._items if not item.isEmpty()]
        return sum(widths) + self._gap * max(0, len(widths) - 1)

    def _arrange(self, rect: QRect, *, move: bool) -> int:
        lines, line, used = [], [], 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if line and used + self._gap + hint.width() > rect.width():
                lines.append(line)
                line, used = [], 0
            used += (self._gap if line else 0) + hint.width()
            line.append((item, hint))
        if line:
            lines.append(line)
        y = rect.y()
        for line in lines:
            height = max(hint.height() for _item, hint in line)
            x = rect.x()
            for item, hint in line:
                if move:
                    item.setGeometry(QRect(x, y + (height - hint.height()) // 2,
                                           min(hint.width(), rect.width()), hint.height()))
                x += hint.width() + self._gap
            y += height + self._gap
        return y - self._gap - rect.y() if lines else 0
