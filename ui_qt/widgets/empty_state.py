"""The one empty-list look Settings uses: a dashed box, centred title and hint."""
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QWidget

from ui_qt.widgets.wrapped_label import WrappedLabel


class EmptyState(QFrame):
    """A dashed, centred box with a short title and a one-line hint.

    ``add_action`` puts a button centred under the hint, for lists whose
    first item is best added from right there.
    """

    def __init__(self, title: str = "", hint: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("emptyState")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(20, 18, 20, 18)
        self._layout.setSpacing(4)
        self.title_label = WrappedLabel("")
        self.title_label.setObjectName("emptyStateTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(self.title_label)
        self.hint_label = WrappedLabel("")
        self.hint_label.setObjectName("emptyStateHint")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(self.hint_label)
        self.set_text(title, hint)

    def set_text(self, title: str, hint: str = "") -> None:
        """Show ``title`` and, when given, ``hint`` under it."""
        self.title_label.setText(title)
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))

    def text(self) -> str:
        """The title and hint as one string, for tests and accessibility."""
        return " ".join(part for part in (self.title_label.text(), self.hint_label.text()) if part)

    def add_action(self, button: QWidget, spacing: Optional[int] = 8) -> None:
        """Put ``button`` centred under the hint."""
        if spacing:
            self._layout.addSpacing(spacing)
        self._layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignHCenter)
