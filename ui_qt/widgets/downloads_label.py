"""Engine messages with an in-app Downloads link that survives elision."""
from html import escape

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import QLabel

from ui_qt.utils.palette import current_palette
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

    _LINK_TARGETS = ("Models & engines", "Downloads")

    def _matched_target(self) -> str:
        for label in self._LINK_TARGETS:
            if self._full_text.endswith(f" in {label}."):
                return label
        return ""

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        target = self._matched_target()
        if target:
            link_width = self.fontMetrics().horizontalAdvance(f"… {target}.")
            hint.setWidth(max(hint.width(), link_width))
        return hint

    def _apply_elide(self) -> None:
        target = self._matched_target()
        has_link = bool(target)
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
        suffix = f" {target}."
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
            f'{escape(prefix)} <a href="downloads" '
            f'style="color: {current_palette().css("accent")}; '
            f'text-decoration: underline;">{escape(target)}</a>.',
        )
