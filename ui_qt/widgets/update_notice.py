"""Title-bar button that stays up while a newer release is waiting."""

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QPushButton

from ui_qt.utils.icons import tabler_icon


class UpdateNoticeButton(QPushButton):
    """Opens the update dialog. Hidden until a check finds a newer release."""

    _STYLE = """
        QPushButton#updateNoticeButton {
            background-color: @accent-tint;
            color: @accent-soft;
            border: 1px solid @accent-tint-border;
            border-radius: 11px;
            padding: 2px 10px 2px 8px;
            margin: 0 8px;
            font-size: 12px;
            font-weight: 600;
        }
        QPushButton#updateNoticeButton:hover {
            background-color: @accent-tint-strong;
            border-color: @accent-tint-border-strong;
        }
        QPushButton#updateNoticeButton:pressed {
            background-color: @accent-muted;
        }
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("updateNoticeButton")
        self.setStyleSheet(self._STYLE)
        self.setIcon(tabler_icon("download-blue.svg"))
        self.setIconSize(QSize(14, 14))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._version = ""
        self._compact = False
        self.hide()

    def show_version(self, version: str) -> None:
        """Offer ``version``; an empty one hides the button."""
        self._version = version
        if not version:
            self.hide()
            return
        self._sync_text()
        self.setToolTip(f"OpenWhisper {version} is available. Click to see what's new.")
        self.setAccessibleName(f"Update to OpenWhisper {version}")
        self.show()

    def set_compact(self, compact: bool) -> None:
        """Drop the version from the label; the compact window has no room."""
        self._compact = bool(compact)
        self._sync_text()

    def _sync_text(self) -> None:
        if self._version:
            self.setText("Update" if self._compact else f"Update to {self._version}")
