"""Shared frame, styling, and interaction for Settings → Downloads rows.

:class:`~ui_qt.widgets.model_row_widget.ModelRowWidget` and
:class:`~ui_qt.widgets.component_row_widget.ComponentRowWidget` read as one
list: the same card, labels, status badge, compact action buttons, and
progress bar, and the same "click or Enter opens details" behaviour. Each row
keeps its own object names, so the stylesheet is built per row from
:func:`row_style`.
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QAbstractButton, QFrame

from ui_qt.widgets.buttons import Button
from ui_qt.utils.restyle import repolish

# Child labels must set an explicit transparent background — the global
# ``QWidget { background-color: @bg }`` rule otherwise paints window-coloured
# rectangles on top of the row fill.
_ROW_STYLE = """
    QFrame#{row} {{
        background-color: @slate-surface;
        border: 1px solid @slate-border;
        border-radius: 12px;
    }}
    QFrame#{row}:hover {{
        background-color: @slate-surface-hover;
        border: 1px solid @slate-border-strong;
    }}
    QFrame#{row}:focus {{
        border: 1px solid @accent-tint-border;
        outline: none;
    }}{state_rules}
    QFrame#{row}[selected="true"],
    QFrame#{row}[selected="true"]:hover {{
        background-color: @accent-tint-strong;
        border: 1px solid @accent-tint-border-strong;
    }}
    QLabel#{row}Name {{
        color: @slate-text;
        background-color: transparent;
        border: none;
        font-weight: 600;
    }}
    QLabel#{row}Summary {{
        color: @slate-text-3;
        background-color: transparent;
        border: none;
    }}
    QLabel#{row}Size {{
        color: @slate-text-2;
        background-color: transparent;
        border: none;
    }}
    QLabel#{row}Badge {{
        background-color: rgba(@slate-text-3-rgb, 0.12);
        color: @slate-text-2;
        border: 1px solid rgba(@slate-text-3-rgb, 0.28);
        border-radius: 6px;
        padding: 2px 8px;
        font-size: 10px;
        font-weight: 600;
    }}
    QLabel#{row}Badge[tone="downloading"] {{
        background-color: rgba(@accent-rgb, 0.14);
        color: @accent-soft;
        border: 1px solid rgba(@accent-rgb, 0.28);
    }}
    QPushButton#{primary},{secondary}
    QPushButton#{remove} {{
        border-radius: 7px;
        padding: 4px 10px;
        font-size: 11px;
        font-weight: 600;
        min-height: 28px;
        max-height: 28px;
    }}
    QPushButton#{primary} {{
        background-color: rgba(@accent-rgb, 0.18);
        color: @accent-soft;
        border: 1px solid rgba(@accent-rgb, 0.32);
    }}
    QPushButton#{primary}:hover {{
        background-color: rgba(@accent-rgb, 0.28);
        border: 1px solid rgba(@accent-rgb, 0.5);
    }}
    QPushButton#{primary}:disabled {{
        background-color: @slate-raised;
        color: @slate-text-disabled;
        border: 1px solid @slate-border-subtle;
    }}
    QPushButton#{remove} {{
        background-color: transparent;
        color: @danger-text-soft;
        border: 1px solid @slate-border-strong;
    }}
    QPushButton#{remove}:hover {{
        background-color: rgba(@danger-rgb, 0.14);
        border: 1px solid rgba(@danger-rgb, 0.45);
    }}
    QProgressBar#{progress} {{
        background-color: @slate-raised;
        border: none;
        border-radius: 3px;
        min-height: 6px;
        max-height: 6px;
        text-align: center;
        color: transparent;
    }}
    QProgressBar#{progress}::chunk {{
        background-color: @accent;
        border-radius: 3px;
    }}
"""


def row_style(
    row: str,
    *,
    primary: str,
    remove: str,
    progress: str,
    secondary: str = "",
    state_rules: str = "",
    extra: str = "",
) -> str:
    """Build a row stylesheet for one row's object names.

    Args:
        row: The row frame's object name; labels are ``<row>Name``,
            ``<row>Summary``, ``<row>Size`` and ``<row>Badge``.
        primary: Object name of the accent action (Download, Install).
        remove: Object name of the destructive action (Delete, Remove).
        progress: Object name of the row's progress bar.
        secondary: Optional neutral action sharing the compact button shape.
        state_rules: Extra frame states; they sit before ``[selected]`` so a
            selected row wins over them.
        extra: Rules appended after the shared ones.
    """
    return _ROW_STYLE.format(
        row=row,
        primary=primary,
        secondary=f"\n    QPushButton#{secondary}," if secondary else "",
        remove=remove,
        progress=progress,
        state_rules=state_rules,
    ) + extra


class DownloadRow(QFrame):
    """A clickable Downloads row that opens its details popup.

    Clicks on the row body, or Enter/Return/Space while it has focus, emit
    ``details_requested`` with the row's id; clicks on its buttons do not.
    """

    details_requested = pyqtSignal(str)

    def __init__(
        self,
        row_id: str,
        *,
        object_name: str,
        style: str,
        tooltip: str,
        accessible_name: str,
        accessible_description: str,
        parent=None,
    ):
        super().__init__(parent)
        self._row_id = row_id
        self.setObjectName(object_name)
        self.setStyleSheet(style)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(tooltip)
        self.setAccessibleName(accessible_name)
        self.setAccessibleDescription(accessible_description)

    @staticmethod
    def _compact_button(button: Button, width: int) -> None:
        """Fix a shared application button to the row's 28 px action size."""
        button.set_base_minimum_size(width, 28)
        button.setMinimumWidth(width)
        button.setMaximumWidth(width)
        button.setMinimumHeight(28)
        button.setMaximumHeight(28)
        button.setFont(QFont("Segoe UI", 10))

    def _set_badge(self, text: str, tone: str) -> None:
        """Update badge text and dynamic tone property for QSS styling."""
        self.badge.setText(text)
        self.badge.setProperty("tone", tone)
        repolish(self.badge)

    @staticmethod
    def _is_action_child(widget) -> bool:
        while widget is not None:
            if isinstance(widget, QAbstractButton):
                return True
            widget = widget.parentWidget()
        return False

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if not self._is_action_child(child):
                self.details_requested.emit(self._row_id)
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.details_requested.emit(self._row_id)
            event.accept()
            return
        super().keyPressEvent(event)
