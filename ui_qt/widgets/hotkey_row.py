"""Hotkeys page cards that move their controls under their text when narrow."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QBoxLayout, QFrame, QHBoxLayout, QVBoxLayout, QWidget

from ui_qt.utils.font_scale import current_ui_font_scale

#: Narrowest the text column may get beside the controls before they move
#: under it, at 100% font scale.
_MIN_COPY_WIDTH = 200


class ReflowCard(QFrame):
    """A card with text on the left and controls on the right, or stacked.

    Fill ``copy`` and ``add_control``; ``add_footer`` adds a full-width row
    underneath. Tiled Omarchy windows are often too narrow for a 220 px
    shortcut field and a Clear button beside the description. Stacked, the
    first control also gives up width before the row would overflow.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 12, 8)
        outer.setSpacing(8)
        self._outer = outer
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._box.setSpacing(16)
        self.copy = QVBoxLayout()
        self.copy.setSpacing(2)
        self.controls = QHBoxLayout()
        self.controls.setSpacing(8)
        self._box.addLayout(self.copy, stretch=1)
        self._box.addLayout(self.controls)
        outer.addLayout(self._box)
        self._controls: list[tuple[QWidget, int]] = []
        self.stacked = False

    def add_control(self, widget: QWidget) -> None:
        self.controls.addWidget(widget, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._controls.append((widget, widget.minimumWidth()))

    def add_footer(self, widget: QWidget) -> None:
        self._outer.addWidget(widget)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow(event.size().width())

    def _reflow(self, width: int) -> None:
        margins = self._outer.contentsMargins()
        inner = width - margins.left() - margins.right()
        widths = [
            max(minimum, widget.sizeHint().width()) for widget, minimum in self._controls
        ]
        spacing = self.controls.spacing() * max(0, len(widths) - 1)
        stacked = inner < round(_MIN_COPY_WIDTH * current_ui_font_scale()) + 16 + sum(widths) + spacing
        if stacked != self.stacked:
            self.stacked = stacked
            self._box.setDirection(
                QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
            )
            self._box.setSpacing(8 if stacked else 16)
            self._box.setAlignment(
                self.controls,
                Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag(0),
            )
            self.updateGeometry()
        if self._controls:
            first, minimum = self._controls[0]
            room = inner - sum(widths[1:]) - spacing
            first.setMinimumWidth(max(0, min(minimum, room)) if stacked else minimum)
