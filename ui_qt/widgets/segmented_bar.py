"""A segmented control: equal-width choices in a tray that wraps when narrow.

Used for the MCP page's computer and assistant choosers and the Remote engine
page's tabs. Each choice is a real checkable button, so it keeps keyboard focus
and accessibility, and may carry a second line of text that can change while
shown (``set_detail``).
"""

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.restyle import set_style_property


class SegmentButton(QPushButton):
    """A native, keyboard-accessible button with independently sized text lines."""

    def __init__(self, title, detail, *, compact=False):
        super().__init__(title)
        self._compact = compact
        self.setObjectName("mcpChoice")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(title)
        self.setAccessibleDescription(detail)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*((12, 4, 12, 4) if compact else (14, 8, 14, 8)))
        layout.setSpacing(2)
        self._layout = layout
        self._detail = None
        self._title = None
        for text, name in ((title, "mcpChoiceTitle"), (detail, "mcpChoiceDetail")):
            if not text:
                continue
            label = self._line(text, name)
            if name == "mcpChoiceDetail":
                self._detail = label
            else:
                self._title = label
        self.toggled.connect(
            lambda checked: set_style_property(
                layout.itemAt(0).widget(), "selected", checked
            )
        )

    def _line(self, text, name):
        label = QLabel(text)
        label.setObjectName(name)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._layout.addWidget(label)
        return label

    def set_title(self, text):
        """The main line, which may change while shown (a count, for one-line tabs)."""
        if self._title is not None and self._title.text() != text:
            self._title.setText(text)
            self.setAccessibleName(text)
            self.updateGeometry()

    def set_detail(self, text):
        """The supporting line under the title, which may change while shown."""
        if self._detail is None:
            self._detail = self._line(text, "mcpChoiceDetail")
        self._detail.setText(text)
        self.setAccessibleDescription(text)
        self.updateGeometry()

    def sizeHint(self):
        return self.layout().sizeHint().expandedTo(QSize(0, 28 if self._compact else 36))

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""  # Child labels provide the title and supporting text.
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


class SegmentedBar(QWidget):
    """A segmented tray. Equal-width choices wrap into rows instead of one tall stack."""

    currentIndexChanged = pyqtSignal(int)
    #: Only a person's click, not ``setCurrentIndex`` called by the page.
    activated = pyqtSignal(int)

    def __init__(self, choices, *, parent=None, compact=False):
        super().__init__(parent)
        self.setObjectName("mcpChoiceBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QGridLayout(self)
        tray = 3 if compact else 4
        layout.setContentsMargins(tray, tray, tray, tray)
        layout.setSpacing(tray)
        self._group = QButtonGroup(self)
        self._index = -1
        self._columns = 0
        self.buttons = []
        for index, (title, detail) in enumerate(choices):
            button = SegmentButton(title, detail, compact=compact)
            self._group.addButton(button, index)
            self.buttons.append(button)
        self._place(len(self.buttons))
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._reflow)
        self._group.idClicked.connect(self.activated)
        self._group.idClicked.connect(self.setCurrentIndex)
        self.setCurrentIndex(0)

    def set_detail(self, index, text):
        if self.buttons[index]._detail is None or self.buttons[index]._detail.text() != text:
            self.buttons[index].set_detail(text)
            self._reflow_timer.start(0)

    def set_title(self, index, text):
        self.buttons[index].set_title(text)
        self._reflow_timer.start(0)

    def _place(self, columns):
        if columns == self._columns:
            return
        self._columns = columns
        layout = self.layout()
        for button in self.buttons:
            layout.removeWidget(button)
        for index, button in enumerate(self.buttons):
            layout.addWidget(button, index // columns, index % columns)
        for column in range(len(self.buttons)):
            layout.setColumnStretch(column, 1 if column < columns else 0)

    def _reflow(self):
        layout = self.layout()
        margins = layout.contentsMargins()
        gap = layout.horizontalSpacing()
        cell = max(button.sizeHint().width() for button in self.buttons)
        for button in self.buttons:
            button.setMinimumWidth(cell)
        room = self.width() - margins.left() - margins.right() + gap
        fits = max(1, min(len(self.buttons), room // (cell + gap)))
        rows = -(-len(self.buttons) // fits)
        # Balance the rows so four choices wrap 2 + 2, never 3 + 1.
        self._place(-(-len(self.buttons) // rows))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ) and hasattr(self, "_reflow_timer"):
            self._reflow_timer.start(0)

    def currentIndex(self):
        return self._index

    def setCurrentIndex(self, index):
        if index == self._index or not 0 <= index < len(self.buttons):
            return
        self._index = index
        self.buttons[index].setChecked(True)
        self.currentIndexChanged.emit(index)
