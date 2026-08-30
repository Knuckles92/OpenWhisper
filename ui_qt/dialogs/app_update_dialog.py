"""Dialog for application update checks.

Shown automatically the first time a newer GitHub release exists, and from
Help → Check for Updates. Opt-out checkboxes persist the same Settings keys
as Settings → General.
"""
from __future__ import annotations

import logging
from typing import Callable, Final, Optional

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from services.app_update import (
    ApplyMode,
    DownloadPhase,
    UpdateCheckResult,
    UpdateStatus,
    channel_label,
    persist_prompt_choices,
)
from services.format_utils import format_size_bytes
from services.settings import (
    resolve_update_check_enabled,
    resolve_update_notify_enabled,
)
from ui_qt.utils.release_notes import render_release_notes_html
from ui_qt.widgets import (
    AnimatedProgressBar,
    Button,
    PrimaryButton,
    WrappedLabel,
)

logger = logging.getLogger(__name__)

_NOTES_MIN_HEIGHT: Final[int] = 56
_NOTES_MAX_HEIGHT: Final[int] = 280

_BUTTON_HEIGHT: Final[int] = 40
_NOTES_FADE_HEIGHT: Final[int] = 22
# Matches QFrame#updateNotesCard in theme.qss so the fade lands on the card.
_NOTES_CARD_COLOR: Final[str] = "#232326"

_PHASE_TEXT: Final[dict] = {
    DownloadPhase.DOWNLOADING: "Downloading the update…",
    DownloadPhase.VERIFYING: "Verifying the download…",
    DownloadPhase.EXTRACTING: "Preparing the update…",
    DownloadPhase.RESTARTING: "Restarting to finish the update…",
    DownloadPhase.ROLLING_BACK: "Restoring the previous version…",
}


class _NotesView(QTextBrowser):
    """Release notes that fade into the card wherever they are clipped.

    The view is capped at a height, so a long changelog is cut mid-line: a
    fade reads as "there is more below", where a sliced row of glyphs just
    reads as a rendering fault.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)

    def paintEvent(self, event):
        super().paintEvent(event)
        scrollbar = self.verticalScrollBar()
        if scrollbar.maximum() <= scrollbar.minimum():
            return
        rect = self.viewport().rect()
        painter = QPainter(self.viewport())
        if scrollbar.value() > scrollbar.minimum():
            self._paint_fade(painter, QRectF(rect), at_top=True)
        if scrollbar.value() < scrollbar.maximum():
            self._paint_fade(painter, QRectF(rect), at_top=False)

    @staticmethod
    def _paint_fade(painter: QPainter, rect: QRectF, at_top: bool) -> None:
        opaque = QColor(_NOTES_CARD_COLOR)
        clear = QColor(opaque)
        clear.setAlpha(0)
        height = min(_NOTES_FADE_HEIGHT, rect.height() / 2.0)
        top = rect.top() if at_top else rect.bottom() - height
        band = QRectF(rect.left(), top, rect.width(), height)
        gradient = QLinearGradient(band.left(), band.top(), band.left(), band.bottom())
        gradient.setColorAt(0.0, opaque if at_top else clear)
        gradient.setColorAt(1.0, clear if at_top else opaque)
        painter.fillRect(band, gradient)


class AppUpdateDialog(QDialog):
    """Update-available / check-result dialog with persistent opt-outs."""

    RESULT_LATER: Final[str] = "later"
    RESULT_PRIMARY: Final[str] = "primary"
    RESULT_USE_SETUP: Final[str] = "use_setup"

    def __init__(
        self,
        result: Optional[UpdateCheckResult],
        error: str = "",
        parent=None,
    ):
        """Initialize the dialog.

        Args:
            result: Latest check outcome, or None when the check failed.
            error: User-facing failure text when ``result`` is None.
            parent: Parent widget (normally the main window).
        """
        super().__init__(parent)
        self.setObjectName("appUpdateDialog")
        self._result = result
        self._error = error or ""
        self._persisted = False
        self.result_action = self.RESULT_LATER
        self._busy = False
        self._cancel_requested = False
        self.on_download_requested: Optional[
            Callable[[UpdateCheckResult], None]
        ] = None
        self.on_cancel_requested: Optional[Callable[[], None]] = None
        self.on_setup_requested: Optional[
            Callable[[UpdateCheckResult], None]
        ] = None

        self.setWindowTitle("OpenWhisper Updates")
        self.setMinimumWidth(520)
        self.setModal(True)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(10)

        self.title_label = WrappedLabel(self._title_text())
        self.title_label.setObjectName("updateTitleLabel")
        layout.addWidget(self.title_label)

        self.body_label = WrappedLabel(self._body_text())
        self.body_label.setObjectName("updateBodyLabel")
        self.body_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        if self._result is None:
            self._set_body_tone("error")
        layout.addWidget(self.body_label)

        self.notes_card = self._build_notes_card()
        layout.addWidget(self.notes_card)

        self.hint_label = WrappedLabel(self._hint_text())
        self.hint_label.setObjectName("updateHintLabel")
        self.hint_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        if not self._hint_text():
            self.hint_label.hide()
        layout.addWidget(self.hint_label)

        self.progress_panel = self._build_progress_panel()
        layout.addWidget(self.progress_panel)

        self.options_widget = QWidget()
        self.options_widget.setObjectName("updateOptions")
        options = QVBoxLayout(self.options_widget)
        options.setContentsMargins(0, 4, 0, 0)
        options.setSpacing(6)

        self.dont_notify_check = QCheckBox("Don't notify me again")
        self.dont_notify_check.setObjectName("updateDontNotifyCheck")
        self.dont_notify_check.setChecked(not resolve_update_notify_enabled())
        options.addWidget(self.dont_notify_check)

        self.dont_check_check = QCheckBox("Don't check automatically")
        self.dont_check_check.setObjectName("updateDontCheckCheck")
        self.dont_check_check.setChecked(not resolve_update_check_enabled())
        options.addWidget(self.dont_check_check)
        layout.addWidget(self.options_widget)

        layout.addStretch(1)

        divider = QFrame()
        divider.setObjectName("updateFooterDivider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 4, 0, 0)
        buttons.setSpacing(8)
        buttons.addStretch()

        self.setup_btn = Button("Use installer instead")
        self.setup_btn.setObjectName("updateSetupButton")
        self._size_footer_button(self.setup_btn)
        self.setup_btn.clicked.connect(self._on_use_setup)
        self.setup_btn.hide()
        buttons.addWidget(self.setup_btn)

        self.later_btn = Button(self._later_label())
        self.later_btn.setObjectName("updateLaterButton")
        self._size_footer_button(self.later_btn)
        self.later_btn.clicked.connect(self._on_later)
        buttons.addWidget(self.later_btn)

        primary = self._primary_label()
        self.primary_btn = PrimaryButton(primary)
        self.primary_btn.setObjectName("updatePrimaryButton")
        self._size_footer_button(self.primary_btn)
        self.primary_btn.clicked.connect(self._on_primary)
        if not primary:
            self.primary_btn.hide()
        else:
            self.primary_btn.setDefault(True)
        buttons.addWidget(self.primary_btn)

        layout.addLayout(buttons)

    @staticmethod
    def _size_footer_button(button: Button) -> None:
        """Fit a footer button to its own label.

        ``Button`` defaults to a 140px floor and only measures its text when
        the text is set again, so three buttons at once overflow the dialog and
        the widest label is clipped.
        """
        button.set_base_minimum_size(0, _BUTTON_HEIGHT)

    def _build_notes_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("updateNotesCard")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(16, 12, 6, 12)
        inner.setSpacing(6)

        caption = QLabel("WHAT'S NEW")
        caption.setObjectName("updateNotesCaption")
        inner.addWidget(caption)

        self.notes_view = _NotesView()
        self.notes_view.setObjectName("updateNotesView")
        self.notes_view.setOpenExternalLinks(True)
        self.notes_view.setFrameShape(QFrame.Shape.NoFrame)
        self.notes_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.notes_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.notes_view.setFixedHeight(_NOTES_MIN_HEIGHT)
        # Qt indents each list level by a full 40px otherwise, which reads as a
        # blockquote rather than a bullet list at this width.
        self.notes_view.document().setIndentWidth(14)
        inner.addWidget(self.notes_view)

        notes_html = self._notes_html()
        if notes_html:
            self.notes_view.setHtml(notes_html)
        else:
            card.hide()
        return card

    def _build_progress_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("updateProgressPanel")
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(16, 14, 16, 14)
        inner.setSpacing(10)

        self.progress_bar = AnimatedProgressBar()
        inner.addWidget(self.progress_bar)

        detail_row = QHBoxLayout()
        detail_row.setContentsMargins(0, 0, 0, 0)
        detail_row.setSpacing(8)

        self.progress_detail = QLabel("")
        self.progress_detail.setObjectName("updateProgressDetail")
        detail_row.addWidget(self.progress_detail)
        detail_row.addStretch()

        self.progress_percent = QLabel("")
        self.progress_percent.setObjectName("updateProgressPercent")
        detail_row.addWidget(self.progress_percent)

        inner.addLayout(detail_row)
        panel.hide()
        return panel

    def _notes_html(self) -> str:
        if (
            self._result is None
            or self._result.status != UpdateStatus.UPDATE_AVAILABLE
            or self._result.release is None
        ):
            return ""
        return render_release_notes_html(self._result.release.notes)

    def _title_text(self) -> str:
        if self._result is None:
            return "Could not check for updates"
        if self._result.status == UpdateStatus.UPDATE_AVAILABLE:
            latest = (
                self._result.release.version if self._result.release else "a new version"
            )
            return f"OpenWhisper {latest} is available"
        if self._result.status == UpdateStatus.DEVELOPMENT:
            return "This is a development build"
        return "OpenWhisper is up to date"

    def _body_text(self) -> str:
        if self._result is None:
            return self._error or "The update server could not be reached."
        channel = channel_label(self._result.channel)
        current = self._result.current_version
        parts = [f"You have {current} ({channel})."]
        release = self._result.release
        if self._result.status == UpdateStatus.UPDATE_AVAILABLE and release:
            size_bytes = self._download_size_bytes()
            if size_bytes:
                parts.append(f"Download size: {format_size_bytes(size_bytes)}.")
        elif self._result.status == UpdateStatus.DEVELOPMENT and release:
            parts.append(
                f"This copy is newer than the latest release ({release.version})."
            )
        elif release:
            parts.append(f"The latest release is {release.version}.")
        return "  ".join(parts)

    def _download_size_bytes(self) -> int:
        if self._result is None or self._result.release is None:
            return 0
        release = self._result.release
        if self._result.apply_mode == ApplyMode.NATIVE:
            asset = release.native_asset
        elif self._result.apply_mode == ApplyMode.SETUP:
            asset = release.setup_asset
        else:
            # Source checkouts and packaged non-Windows builds are notify-only;
            # showing the Windows setup size there implies the wrong download.
            asset = None
        return int(asset.size_bytes) if asset and asset.size_bytes else 0

    def _hint_text(self) -> str:
        if self._result is None:
            return ""
        if self._result.status != UpdateStatus.UPDATE_AVAILABLE:
            return ""
        parts = []
        if self._result.git_summary:
            parts.append(f"Local git: {self._result.git_summary}")
        if self._result.git_hint:
            parts.append("To update this checkout:\n" + self._result.git_hint)
        return "\n\n".join(parts)

    def _primary_label(self) -> str:
        if self._result is None:
            return "Open releases page"
        if self._result.status != UpdateStatus.UPDATE_AVAILABLE:
            return ""
        if self._result.can_apply:
            return "Download and install"
        return "Open release notes"

    def _later_label(self) -> str:
        if self._result is None or self._result.status != UpdateStatus.UPDATE_AVAILABLE:
            return "Close"
        return "Later"

    def _on_later(self) -> None:
        if self._busy:
            self._on_cancel()
            return
        self.result_action = self.RESULT_LATER
        self._persist(skipped=True)
        self.reject()

    def _on_cancel(self) -> None:
        """Ask the worker to stop; a second press dismisses the dialog.

        A cancel can arrive after the worker's last cancellation point, so
        "canceling" must never be a state the dialog can be left in with no
        working button — the download unwinds on its own once the dialog is
        gone.
        """
        if self._cancel_requested:
            self._busy = False
            self.reject()
            return
        self._cancel_requested = True
        if self.on_cancel_requested:
            self.on_cancel_requested()
        self.body_label.setText("Canceling the update…")
        self.progress_percent.setText("")
        self.progress_bar.set_indeterminate()
        self.later_btn.setText("Close")

    def _on_use_setup(self) -> None:
        if self._result is None or not self.on_setup_requested:
            return
        self.result_action = self.RESULT_USE_SETUP
        self.setup_btn.hide()
        self._set_downloading()
        self.on_setup_requested(self._result)

    def _on_primary(self) -> None:
        self.result_action = self.RESULT_PRIMARY
        self._persist(skipped=False)
        if (
            self._result is not None
            and self._result.can_apply
            and self.on_download_requested
        ):
            self._set_downloading()
            self.on_download_requested(self._result)
            return
        self.accept()

    def reject(self) -> None:
        """Treat window-close as Later so the opt-out boxes still persist."""
        if self._busy:
            self._on_cancel()
            return
        if self.result_action not in (
            self.RESULT_PRIMARY,
            self.RESULT_USE_SETUP,
        ):
            self.result_action = self.RESULT_LATER
            self._persist(skipped=True)
        super().reject()

    def _persist(self, skipped: bool) -> None:
        if self._persisted:
            return
        self._persisted = True
        skipped_version = None
        if (
            skipped
            and self._result is not None
            and self._result.status == UpdateStatus.UPDATE_AVAILABLE
            and self._result.release is not None
        ):
            skipped_version = self._result.release.version
        try:
            persist_prompt_choices(
                notify_enabled=not self.dont_notify_check.isChecked(),
                check_enabled=not self.dont_check_check.isChecked(),
                skipped_version=skipped_version,
            )
        except Exception as exc:
            logger.warning("Could not persist update preferences: %s", exc)

    def _set_downloading(self) -> None:
        self._busy = True
        self._cancel_requested = False
        self.primary_btn.setEnabled(False)
        self.setup_btn.setEnabled(False)
        self.later_btn.setEnabled(True)
        self.later_btn.setText("Cancel")
        self.dont_notify_check.setEnabled(False)
        self.dont_check_check.setEnabled(False)
        self._set_body_tone("")
        self.notes_card.hide()
        self.hint_label.hide()
        self.options_widget.hide()
        self.progress_detail.setText(
            self._progress_detail_text(DownloadPhase.DOWNLOADING, 0, 0)
        )
        self.progress_percent.setText("")
        self.progress_bar.set_indeterminate()
        self.progress_panel.show()
        self.body_label.setText(_PHASE_TEXT[DownloadPhase.DOWNLOADING])
        self._resize_to_content()

    def set_progress(self, phase: str, done: int, total: int) -> None:
        """Update the download progress bar from a worker signal."""
        if phase == DownloadPhase.RESTARTING:
            self.mark_handed_off()
        self.progress_panel.show()
        if total > 0:
            fraction = max(0.0, min(1.0, done / total))
            self.progress_bar.set_fraction(fraction)
            self.progress_percent.setText(f"{int(fraction * 100)}%")
        else:
            self.progress_bar.set_indeterminate()
            self.progress_percent.setText("")
        self.progress_detail.setText(self._progress_detail_text(phase, done, total))
        text = _PHASE_TEXT.get(phase)
        if text:
            self.body_label.setText(text)

    def mark_handed_off(self) -> None:
        """The updater owns the install now, so nothing here can cancel it.

        Clearing ``_busy`` also lets the dialog close on request: a dialog that
        refuses to close vetoes ``QApplication.quit()``, and the app has to be
        able to exit for the updater to make progress.
        """
        self._busy = False
        self._cancel_requested = False
        self.later_btn.setEnabled(False)

    def _set_body_tone(self, tone: str) -> None:
        if self.body_label.property("tone") == tone:
            return
        self.body_label.setProperty("tone", tone)
        self.body_label.style().unpolish(self.body_label)
        self.body_label.style().polish(self.body_label)

    def _progress_detail_text(self, phase: str, done: int, total: int) -> str:
        # Only the download phase counts bytes; the others count archive
        # members, which would read as a nonsense size, so they name the
        # release being installed instead.
        if phase == DownloadPhase.DOWNLOADING and total > 0:
            return f"{format_size_bytes(done)} of {format_size_bytes(total)}"
        if self._result is not None and self._result.release is not None:
            return f"OpenWhisper {self._result.release.version}"
        return ""

    def set_error(self, message: str, offer_setup: bool = False) -> None:
        """Re-enable the dialog after a failed download or prepare."""
        self._busy = False
        self._cancel_requested = False
        self._set_body_tone("error")
        self.body_label.setText(message or "The download failed.")
        self.progress_panel.hide()
        self.progress_bar.reset()
        self.options_widget.show()
        if self._notes_html():
            self.notes_card.show()
        if self._hint_text():
            self.hint_label.show()
        self.primary_btn.setEnabled(True)
        self.later_btn.setEnabled(True)
        self.later_btn.setText(self._later_label())
        self.dont_notify_check.setEnabled(True)
        self.dont_check_check.setEnabled(True)
        self.result_action = self.RESULT_LATER
        can_offer_setup = (
            offer_setup
            and self._result is not None
            and self._result.apply_mode == ApplyMode.NATIVE
            and self._result.release is not None
            and self._result.release.setup_asset is not None
            and self._result.release.setup_asset.sha256
        )
        self.setup_btn.setVisible(bool(can_offer_setup))
        self.setup_btn.setEnabled(True)
        self._resize_to_content()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_notes_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_notes_height()

    def _fit_notes_height(self) -> None:
        """Size the notes to their text, up to the point where they scroll.

        A short changelog in a fixed-height box is mostly empty background, and
        a long one has to scroll somewhere, so the view tracks its own document
        height between a floor and a ceiling.
        """
        if self.notes_card.isHidden():
            return
        document = self.notes_view.document()
        width = self.notes_view.viewport().width()
        if width <= 0:
            return
        document.setTextWidth(width)
        needed = int(document.size().height() + 2 * document.documentMargin())
        height = max(_NOTES_MIN_HEIGHT, min(_NOTES_MAX_HEIGHT, needed))
        if height != self.notes_view.height():
            self.notes_view.setFixedHeight(height)

    def _resize_to_content(self) -> None:
        """Shrink the window when a section is hidden.

        Hiding the notes only lowers the layout's minimum; the window keeps the
        height it was given, which left the download state as a title stranded
        above a field of empty background.
        """
        if not self.isVisible():
            return
        self.layout().activate()
        width = max(self.width(), self.minimumSizeHint().width())
        self.resize(width, self.sizeHint().height())
