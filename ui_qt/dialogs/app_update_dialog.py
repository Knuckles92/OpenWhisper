"""Dialog for application update checks.

Shown automatically the first time a newer GitHub release exists, and from
Help → Check for Updates. Opt-out checkboxes persist the same Settings keys
as Settings → General.
"""
from __future__ import annotations

import logging
from typing import Callable, Final, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
)

from services.app_update import (
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
from ui_qt.widgets import Button, PrimaryButton

logger = logging.getLogger(__name__)

_NOTES_LIMIT: Final[int] = 600


class AppUpdateDialog(QDialog):
    """Update-available / check-result dialog with persistent opt-outs."""

    RESULT_LATER: Final[str] = "later"
    RESULT_PRIMARY: Final[str] = "primary"

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
        self.on_download_requested: Optional[
            Callable[[UpdateCheckResult], None]
        ] = None

        self.setWindowTitle("OpenWhisper Updates")
        self.setMinimumWidth(480)
        self.setModal(True)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        self.title_label = QLabel(self._title_text())
        self.title_label.setObjectName("headerLabel")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.body_label = QLabel(self._body_text())
        self.body_label.setObjectName("updateBodyLabel")
        self.body_label.setWordWrap(True)
        self.body_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.body_label)

        self.hint_label = QLabel(self._hint_text())
        self.hint_label.setObjectName("updateHintLabel")
        self.hint_label.setWordWrap(True)
        self.hint_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        if not self._hint_text():
            self.hint_label.hide()
        layout.addWidget(self.hint_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("updateProgressBar")
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self.dont_notify_check = QCheckBox("Don't notify me again")
        self.dont_notify_check.setObjectName("updateDontNotifyCheck")
        self.dont_notify_check.setChecked(not resolve_update_notify_enabled())
        layout.addWidget(self.dont_notify_check)

        self.dont_check_check = QCheckBox("Don't check automatically")
        self.dont_check_check.setObjectName("updateDontCheckCheck")
        self.dont_check_check.setChecked(not resolve_update_check_enabled())
        layout.addWidget(self.dont_check_check)

        layout.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch()

        self.later_btn = Button(self._later_label())
        self.later_btn.setObjectName("updateLaterButton")
        self.later_btn.clicked.connect(self._on_later)
        buttons.addWidget(self.later_btn)

        primary = self._primary_label()
        self.primary_btn = PrimaryButton(primary)
        self.primary_btn.setObjectName("updatePrimaryButton")
        self.primary_btn.clicked.connect(self._on_primary)
        if not primary:
            self.primary_btn.hide()
        else:
            self.primary_btn.setDefault(True)
        buttons.addWidget(self.primary_btn)

        layout.addLayout(buttons)

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
        lines = [f"You have {current} ({channel})."]
        release = self._result.release
        if self._result.status == UpdateStatus.UPDATE_AVAILABLE and release:
            if release.asset and release.asset.size_bytes:
                lines.append(
                    f"Download size: {format_size_bytes(release.asset.size_bytes)}."
                )
            excerpt = _notes_excerpt(release.notes)
            if excerpt:
                lines.append("")
                lines.append(excerpt)
        elif self._result.status == UpdateStatus.DEVELOPMENT and release:
            lines.append(
                f"This copy is newer than the latest release ({release.version})."
            )
        elif release:
            lines.append(f"The latest release is {release.version}.")
        return "\n".join(lines)

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
        self.result_action = self.RESULT_LATER
        self._persist(skipped=True)
        self.reject()

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
        if self.result_action != self.RESULT_PRIMARY:
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
        self.primary_btn.setEnabled(False)
        self.later_btn.setEnabled(False)
        self.dont_notify_check.setEnabled(False)
        self.dont_check_check.setEnabled(False)
        self.progress_bar.show()
        self.progress_bar.setRange(0, 0)
        self.body_label.setText("Downloading the installer…")

    def set_progress(self, phase: str, done: int, total: int) -> None:
        """Update the download progress bar from a worker signal."""
        self.progress_bar.show()
        if total > 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(max(0, min(100, int(done * 100 / total))))
        else:
            self.progress_bar.setRange(0, 0)
        if phase == "verifying":
            self.body_label.setText("Verifying the download…")

    def set_error(self, message: str) -> None:
        """Re-enable the dialog after a failed download."""
        self.body_label.setText(message or "The download failed.")
        self.progress_bar.hide()
        self.primary_btn.setEnabled(True)
        self.later_btn.setEnabled(True)
        self.dont_notify_check.setEnabled(True)
        self.dont_check_check.setEnabled(True)
        self.result_action = self.RESULT_LATER


def _notes_excerpt(notes: str) -> str:
    text = (notes or "").strip()
    if not text:
        return ""
    if len(text) <= _NOTES_LIMIT:
        return text
    return text[:_NOTES_LIMIT].rstrip() + "…"
