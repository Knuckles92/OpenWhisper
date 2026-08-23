from PyQt6.QtWidgets import QToolButton
from PyQt6.QtCore import Qt, pyqtSignal


class CollapsibleSectionToggle(QToolButton):
    """Centered text toggle used for expandable/collapsible sections.

    Displays ``{prefix}{title}  {arrow}`` where the arrow indicates state.
    The entire label is the click target.
    """

    toggled_expanded = pyqtSignal(bool)

    TOGGLE_STYLE = (
        "QToolButton { color: #8e8e93; border: none; font-size: 11px; "
        "font-weight: 600; }"
        "QToolButton:hover { color: #f5f5f7; }"
    )

    def __init__(
        self,
        title: str,
        *,
        prefix: str = "",
        expanded: bool = False,
        expand_tooltip: str = "Show section",
        collapse_tooltip: str = "Hide section",
        parent=None,
    ):
        super().__init__(parent)
        self._title = title
        self._prefix = prefix
        self._expanded = expanded
        self._expand_tooltip = expand_tooltip
        self._collapse_tooltip = collapse_tooltip

        self.setCheckable(True)
        self.setChecked(expanded)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setStyleSheet(self.TOGGLE_STYLE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_display()
        self.toggled.connect(self._on_toggled)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_title(self, title: str):
        self._title = title
        self._update_display()

    def set_expanded(self, expanded: bool, emit: bool = True):
        """Set expanded state without necessarily emitting a signal.

        Args:
            expanded: True to show expanded arrow/state, False to collapse.
            emit: Whether to emit ``toggled_expanded`` when the state changes.
        """
        if expanded == self._expanded:
            return

        self._expanded = expanded
        self.blockSignals(True)
        self.setChecked(expanded)
        self.blockSignals(False)
        self._update_display()

        if emit:
            self.toggled_expanded.emit(expanded)

    def _on_toggled(self, checked: bool):
        if checked == self._expanded:
            return
        self._expanded = checked
        self._update_display()
        self.toggled_expanded.emit(checked)

    def _update_display(self):
        arrow = "▾" if self._expanded else "▸"
        self.setText(f"{self._prefix}{self._title}  {arrow}")
        self.setToolTip(
            self._collapse_tooltip if self._expanded else self._expand_tooltip
        )
