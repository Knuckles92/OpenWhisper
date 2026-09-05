"""Engine messages with an in-app Downloads link that survives elision."""
from html import escape

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import QLabel

from ui_qt.widgets.eliding_label import ElidingLabel


class DownloadsLabel(ElidingLabel):
    downloads_requested = pyqtSignal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent=parent)
        self.setOpenExternalLinks(False)
        self.linkActivated.connect(self._on_link_activated)

    def _on_link_activated(self, link: str) -> None:
        if link == "downloads":
            self.downloads_requested.emit()

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        if self._full_text.endswith(" in Downloads."):
            link_width = self.fontMetrics().horizontalAdvance("… Downloads.")
            hint.setWidth(max(hint.width(), link_width))
        return hint

    def _apply_elide(self) -> None:
        has_link = self._full_text.endswith(" in Downloads.")
        self.setTextFormat(
            Qt.TextFormat.RichText if has_link else Qt.TextFormat.PlainText
        )
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
            if has_link else Qt.TextInteractionFlag.NoTextInteraction
        )
        if not has_link:
            super()._apply_elide()
            return

        metrics = self.fontMetrics()
        suffix = " Downloads."
        prefix = self._full_text[:-len(suffix)]
        available = (
            self.contentsRect().width() or metrics.horizontalAdvance(self._full_text)
        )
        # Reserve the action's width before shortening the explanation.
        prefix = metrics.elidedText(
            prefix, Qt.TextElideMode.ElideRight,
            max(0, available - metrics.horizontalAdvance(suffix)),
        )
        QLabel.setText(
            self,
            f'{escape(prefix)} <a href="downloads" style="color: #0a84ff; '
            'text-decoration: underline;">Downloads</a>.',
        )
