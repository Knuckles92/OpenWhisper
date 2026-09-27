"""A compact, responsive entry point to the paired host's model library."""

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui_qt.widgets.wrapped_label import WrappedLabel


class RemoteModelNotice(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("remoteModelNotice")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._stacked = True

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(12, 12, 12, 12)
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(10)
        self._grid.setColumnStretch(1, 1)

        self._icon = QLabel()
        self._icon.setObjectName("remoteModelNoticeIcon")
        self._icon.setFixedSize(32, 32)
        self._grid.addWidget(self._icon, 0, 0, Qt.AlignmentFlag.AlignTop)

        copy = QWidget()
        copy.setObjectName("remoteModelNoticeCopy")
        copy.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        copy_layout = QVBoxLayout(copy)
        copy_layout.setContentsMargins(0, 0, 0, 0)
        copy_layout.setSpacing(4)
        self.title_label = WrappedLabel()
        self.title_label.setObjectName("remoteModelNoticeTitle")
        self.detail_label = WrappedLabel()
        self.detail_label.setObjectName("remoteModelNoticeDetail")
        for label in (self.title_label, self.detail_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
            copy_layout.addWidget(label)
        self._grid.addWidget(copy, 0, 1)

        self.manage_button = QPushButton("Manage host models")
        self.manage_button.setObjectName("remoteModelNoticeButton")
        self.manage_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manage_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.manage_button.setToolTip("Browse models and install speech runtimes on the paired host.")
        self._grid.addWidget(self.manage_button, 1, 1, Qt.AlignmentFlag.AlignLeft)
        self.set_available([])

    def set_available(self, names: list[str]) -> None:
        self.title_label.setText("Available to install" if names else "Host model library")
        self.detail_label.setText(
            ", ".join(names) if names else
            "Browse models, manage downloads, and set up CPU or GPU."
        )
        self.manage_button.setAccessibleDescription(self.detail_label.text())
        self._reflow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def event(self, event):
        result = super().event(event)
        # Font/theme changes can alter the button's width without resizing us.
        if event.type() == QEvent.Type.LayoutRequest:
            self._reflow()
        return result

    def _reflow(self) -> None:
        if not hasattr(self, "manage_button"):
            return
        margins = self._grid.contentsMargins()
        # Reserve a readable measure for the copy, using the actual font so
        # larger accessibility sizes switch to the stacked layout earlier.
        copy_width = max(
            self.title_label.fontMetrics().horizontalAdvance(self.title_label.text()),
            self.detail_label.fontMetrics().averageCharWidth() * 34,
        )
        needed = (
            margins.left() + margins.right() + self._icon.width()
            + 2 * self._grid.horizontalSpacing() + copy_width
            + self.manage_button.sizeHint().width()
        )
        stacked = self.contentsRect().width() < needed
        if stacked == self._stacked:
            return
        self._stacked = stacked
        self._grid.removeWidget(self.manage_button)
        self._grid.addWidget(
            self.manage_button, 1 if stacked else 0, 1 if stacked else 2,
            Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag.AlignVCenter,
        )
