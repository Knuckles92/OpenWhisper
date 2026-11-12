"""A control column beside a stage: the layout the Walkthrough and Meeting tabs share.

The column holds what you set; the stage shows what is happening. Neither
moves the other, and the stage changing what it shows never resizes the window.
"""
from __future__ import annotations

from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QBoxLayout, QSizePolicy, QWidget

from ui_qt.utils.font_scale import current_ui_font_scale

# Sizes at 100% type; each grows with the font scale.
#: The control column.
COLUMN_WIDTH = 320
#: Between the column and the stage.
GAP = 16
#: The stage at its smallest, and the width it asks for beside the column.
STAGE_MIN = QSize(400, 520)
STAGE_WIDTH = 704


def scaled(value: int) -> int:
    """``value`` pixels at 100% type, at the type size in use now."""
    return round(value * current_ui_font_scale())


class Stage(QWidget):
    """The right side's one slot: whichever pane shows fills it.

    It has no layout on purpose. A layout would pass each pane's size hint up
    to the window, which would then resize as the stage went from one pane
    to the next.
    """

    def __init__(self, object_name: str = "walkthroughStage", width: int = STAGE_WIDTH,
                 height: int | None = None):
        super().__init__()
        self.setObjectName(object_name)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._width = width
        #: At 100% type, growing with it; None keeps the fixed minimum.
        self._height = height
        self._panes: list[QWidget] = []
        self.current: QWidget | None = None

    def add(self, pane: QWidget) -> QWidget:
        pane.setParent(self)
        pane.hide()
        self._panes.append(pane)
        return pane

    def show_only(self, pane: QWidget) -> None:
        self.current = pane
        pane.setGeometry(self.rect())
        for each in self._panes:
            each.setVisible(each is pane)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for pane in self._panes:
            pane.setGeometry(self.rect())

    def _stage_height(self) -> int:
        return STAGE_MIN.height() if self._height is None else scaled(self._height)

    def sizeHint(self) -> QSize:
        return QSize(scaled(self._width), self._stage_height())

    def minimumSizeHint(self) -> QSize:
        return QSize(scaled(STAGE_MIN.width()), self._stage_height())


class StudioSplit(QWidget):
    """The control column beside the stage, or above it once both don't fit.

    Its own minimum width is ignored, as ``Reflow``'s is: the width comes
    from the page, so a narrow tiled window stacks the two instead of
    clipping the stage at the right edge.
    """

    def __init__(self, column: QWidget, stage: Stage, parent=None,
                 object_name: str = "walkthroughStudio"):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.column = column
        self.stage = stage
        self.column_width = COLUMN_WIDTH
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(GAP)
        self._box.addWidget(column)
        self._box.addWidget(stage, 1)
        self._stacked: bool | None = None
        self._place(False)

    @property
    def stacked(self) -> bool:
        return bool(self._stacked)

    def set_column_width(self, width: int) -> None:
        self.column_width = width
        self._stacked = None
        self.refit()

    def _place(self, stacked: bool) -> None:
        if stacked == self._stacked:
            return
        self._stacked = stacked
        self._box.setDirection(
            QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
        if stacked:
            self.column.setMinimumWidth(0)
            self.column.setMaximumWidth(16777215)
            self.column.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        else:
            self.column.setFixedWidth(self.column_width)
            self.column.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.updateGeometry()

    def refit(self) -> None:
        self.place_for_width(self.width())

    def place_for_width(self, width: int) -> None:
        """Lay the two out as they sit once this is ``width`` wide.

        The window calls it before measuring a page it is about to widen, so
        it measures the layout it will show and goes there in one move.
        """
        needed = self.column_width + GAP + self.stage.minimumSizeHint().width()
        self._place(0 < width < needed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refit()
