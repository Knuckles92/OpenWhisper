"""A row whose trailing actions move onto their own line when it gets narrow."""
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGridLayout, QSizePolicy, QWidget

from ui_qt.utils.font_scale import current_ui_font_scale


class Reflow(QWidget):
    """A lead widget with actions beside it, or below it once the row is narrow.

    The width comes from the column the row sits in (its own minimum is
    ignored), so a narrow tiled window or a large font moves the actions to
    their own line instead of pushing them past the edge, or holding the
    whole page wider than its window.

    Args:
        lead: Takes the spare width.
        actions: Kept at their natural width.
        lead_width: The narrowest the lead may get beside the actions, in
            pixels at 100% font scale. ``None`` keeps the lead at its own
            width, measured in the current font, for a lead that can't shrink
            (a button or a checkbox).
        stacked_alignment: Where the actions sit on their own line.
    """

    def __init__(
        self,
        lead: QWidget,
        actions: QWidget,
        lead_width: Optional[int] = None,
        parent=None,
        *,
        stacked_alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignRight,
    ):
        super().__init__(parent)
        self.setObjectName("reflow")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._lead = lead
        self._actions = actions
        self._lead_width = lead_width
        self._stacked_alignment = stacked_alignment
        self._stacked: Optional[bool] = None
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(6)
        self._grid.addWidget(lead, 0, 0)
        self._grid.setColumnStretch(0, 1)
        self._place(False)

    @property
    def stacked(self) -> bool:
        return bool(self._stacked)

    def _place(self, stacked: bool) -> None:
        if stacked == self._stacked:
            return
        self._stacked = stacked
        self._grid.removeWidget(self._actions)
        if stacked:
            self._grid.addWidget(self._actions, 1, 0, alignment=self._stacked_alignment)
        else:
            self._grid.addWidget(self._actions, 0, 1, alignment=Qt.AlignmentFlag.AlignVCenter)

    @staticmethod
    def _natural_width(widget: QWidget) -> int:
        # A label-fitted button can hold a floor above its size hint.
        return max(widget.sizeHint().width(), widget.minimumWidth())

    def _reflow(self) -> None:
        lead = (
            self._natural_width(self._lead) if self._lead_width is None
            else round(self._lead_width * current_ui_font_scale())
        )
        needed = lead + self._grid.horizontalSpacing() + self._natural_width(self._actions)
        self._place(self.width() < needed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()
