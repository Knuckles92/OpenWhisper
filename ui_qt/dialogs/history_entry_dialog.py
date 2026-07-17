"""Dialog for viewing a past transcription history entry."""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import QLineF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from services.format_utils import format_file_size
from services.history_manager import HistoryEntry, history_manager
from ui_qt.widgets import Button, DangerButton, PrimaryButton, WarningButton
from ui_qt.widgets.history_sidebar import (
    _MENU_STYLESHEET,
    _entry_was_cleaned,
    _format_cleanup_info,
    _format_model_name,
)

logger = logging.getLogger(__name__)

_DIALOG_STYLE = """
    QFrame#historyEntrySection {
        background-color: rgba(44, 44, 46, 0.55);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 10px;
    }
    QLabel#historyEntryTitle {
        color: #f5f5f7;
        background-color: transparent;
        font-size: 18px;
        font-weight: 700;
    }
    QLabel#historyEntryChip {
        color: #6fb1ff;
        background-color: rgba(10, 132, 255, 0.12);
        border: 1px solid rgba(10, 132, 255, 0.25);
        border-radius: 6px;
        padding: 3px 8px;
        font-size: 10px;
        font-weight: 600;
    }
    QLabel#historyEntryCleanupChip {
        color: #30d158;
        background-color: rgba(48, 209, 88, 0.12);
        border: 1px solid rgba(48, 209, 88, 0.28);
        border-radius: 6px;
        padding: 3px 8px;
        font-size: 10px;
        font-weight: 600;
    }
    QLabel#historyEntrySectionTitle {
        color: #f5f5f7;
        background-color: transparent;
        border: none;
        font-size: 12px;
        font-weight: 700;
    }
    QLabel#historyEntryFactLabel {
        color: #8e8e93;
        background-color: transparent;
        border: none;
        font-size: 10px;
        font-weight: 600;
    }
    QLabel#historyEntryFactValue {
        color: #d1d1d6;
        background-color: transparent;
        border: none;
        font-size: 11px;
    }
    QWidget#historyRetranscribeSplit QPushButton#warningButton {
        border-top-right-radius: 0px;
        border-bottom-right-radius: 0px;
        padding-left: 20px;
        padding-right: 18px;
    }
    QWidget#historyRetranscribeSplit QToolButton#historyRetranscribeMenu {
        background-color: #ff9f0a;
        border: none;
        border-top-left-radius: 0px;
        border-bottom-left-radius: 0px;
        border-top-right-radius: 8px;
        border-bottom-right-radius: 8px;
        border-left: 1px solid rgba(255, 255, 255, 0.22);
        min-width: 38px;
        max-width: 38px;
        min-height: 40px;
        padding: 0px;
    }
    QWidget#historyRetranscribeSplit QToolButton#historyRetranscribeMenu:hover {
        background-color: #ff9500;
    }
    QWidget#historyRetranscribeSplit QToolButton#historyRetranscribeMenu:pressed {
        background-color: #cc7700;
    }
    QWidget#historyRetranscribeSplit QToolButton#historyRetranscribeMenu::menu-indicator {
        image: none;
        width: 0px;
    }
"""


def _chevron_down_icon() -> QIcon:
    """Return a crisp, platform-independent white chevron icon."""
    pixmap = QPixmap(32, 32)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#ffffff"), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawLines((
        QLineF(4.5, 6.5, 8.0, 10.0),
        QLineF(8.0, 10.0, 11.5, 6.5),
    ))
    painter.end()
    return QIcon(pixmap)


def _format_seconds(seconds: float) -> str:
    """Format a duration in seconds for compact display."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remainder = seconds % 60
    return f"{minutes}m {remainder:.0f}s"


class HistoryEntryDialog(QDialog):
    """Scrollable viewer for a single history transcription."""

    copied = pyqtSignal()
    # audio_path, skip_cleanup (raw ASR only — no AI cleanup pass)
    retranscribe_requested = pyqtSignal(str, bool)
    delete_requested = pyqtSignal(str)

    def __init__(self, entry: HistoryEntry, parent=None):
        """Initialize the history entry viewer.

        Args:
            entry: History entry to display.
            parent: Owning window.
        """
        super().__init__(parent)
        self.entry = entry
        self._fixed_text = entry.text or ""
        self._raw_text: Optional[str] = (
            entry.raw_text
            if entry.raw_text and entry.raw_text != entry.text
            else None
        )
        self._showing_raw = False
        self._audio_path: Optional[str] = None
        if entry.audio_file:
            path = history_manager.get_recording_path(entry.audio_file)
            if path:
                self._audio_path = path

        self.setWindowTitle("Transcription")
        self.setModal(True)
        self.setMinimumSize(640, 600)
        self.resize(720, 700)
        self.setStyleSheet(_DIALOG_STYLE)

        self._setup_ui()
        self._setup_shortcuts()

    def _setup_ui(self) -> None:
        """Build header, metadata, transcript body, and action footer."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel(self.entry.formatted_timestamp)
        title.setObjectName("historyEntryTitle")
        title.setAccessibleName("Transcription timestamp")
        header.addWidget(title)
        header.addStretch()

        model_chip = QLabel(_format_model_name(self.entry.model))
        model_chip.setObjectName("historyEntryChip")
        model_chip.setToolTip(self.entry.model)
        model_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(model_chip)

        if _entry_was_cleaned(self.entry):
            cleanup_chip = QLabel(f"✦ {_format_cleanup_info(self.entry)}")
            cleanup_chip.setObjectName("historyEntryCleanupChip")
            cleanup_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if self.entry.cleanup_model:
                cleanup_chip.setToolTip(
                    f"Transcript cleaned with {_format_cleanup_info(self.entry)}"
                )
            else:
                cleanup_chip.setToolTip(
                    "Transcript was cleaned (model not recorded)"
                )
            header.addWidget(cleanup_chip)

        outer.addLayout(header)

        metadata = self._build_metadata_section()
        if metadata is not None:
            outer.addWidget(metadata)

        body = QFrame()
        body.setObjectName("historyEntrySection")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(14, 12, 14, 12)
        body_layout.setSpacing(8)

        body_header = QHBoxLayout()
        body_header.setContentsMargins(0, 0, 0, 0)
        body_header.setSpacing(8)
        section_title = QLabel("Transcript")
        section_title.setObjectName("historyEntrySectionTitle")
        body_header.addWidget(section_title)
        body_header.addStretch()

        self.version_toggle = QWidget()
        version_row = QHBoxLayout(self.version_toggle)
        version_row.setContentsMargins(0, 0, 0, 0)
        version_row.setSpacing(6)
        self._version_group = QButtonGroup(self)
        self.fixed_btn = QPushButton("Fixed")
        self.raw_btn = QPushButton("Raw")
        for btn in (self.fixed_btn, self.raw_btn):
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setObjectName("transcriptVersionBtn")
            btn.setMinimumHeight(28)
            self._version_group.addButton(btn)
            version_row.addWidget(btn)
        self.fixed_btn.setChecked(True)
        self.fixed_btn.toggled.connect(self._on_version_toggled)
        self.raw_btn.toggled.connect(self._on_version_toggled)
        self.version_toggle.setVisible(self._raw_text is not None)
        body_header.addWidget(self.version_toggle)
        body_layout.addLayout(body_header)

        self.transcript_text = QTextEdit()
        self.transcript_text.setObjectName("historyEntryTranscript")
        self.transcript_text.setReadOnly(True)
        self.transcript_text.setFont(QFont("Segoe UI", 13))
        self.transcript_text.setText(self._fixed_text)
        self.transcript_text.setMinimumHeight(240)
        body_layout.addWidget(self.transcript_text, stretch=1)
        outer.addWidget(body, stretch=1)

        # Two rows so long labels never collide at the default dialog width.
        # Visual roles: Copy = primary, Retranscribe = warning, Delete = danger,
        # others = neutral secondary.
        primary_actions = QHBoxLayout()
        primary_actions.setSpacing(8)

        self.copy_button = PrimaryButton("Copy")
        self.copy_button.setToolTip("Copy the currently shown transcript (Ctrl+C)")
        self.copy_button.set_base_minimum_size(96, 40)
        self.copy_button.clicked.connect(self._copy_shown_text)
        primary_actions.addWidget(self.copy_button)

        self.copy_raw_button = Button("Copy Raw")
        self.copy_raw_button.setToolTip("Copy the unprocessed ASR transcript")
        self.copy_raw_button.set_base_minimum_size(96, 40)
        self.copy_raw_button.clicked.connect(self._copy_raw_text)
        self.copy_raw_button.setVisible(self._raw_text is not None)
        primary_actions.addWidget(self.copy_raw_button)

        self.retranscribe_split = QWidget()
        self.retranscribe_split.setObjectName("historyRetranscribeSplit")
        split_layout = QHBoxLayout(self.retranscribe_split)
        split_layout.setContentsMargins(0, 0, 0, 0)
        split_layout.setSpacing(0)

        self.retranscribe_button = WarningButton("Transcribe Again")
        self.retranscribe_button.setToolTip(
            "Run transcription again on the saved recording"
        )
        self.retranscribe_button.set_base_minimum_size(96, 40)
        self.retranscribe_button.clicked.connect(
            lambda: self._on_retranscribe(skip_cleanup=False)
        )
        split_layout.addWidget(self.retranscribe_button)

        self.retranscribe_menu_button = QToolButton()
        self.retranscribe_menu_button.setObjectName("historyRetranscribeMenu")
        self.retranscribe_menu_button.setIcon(_chevron_down_icon())
        self.retranscribe_menu_button.setIconSize(QSize(16, 16))
        self.retranscribe_menu_button.setAccessibleName("More transcription options")
        self.retranscribe_menu_button.setToolTip("More retranscribe options")
        self.retranscribe_menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retranscribe_menu_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        retranscribe_menu = QMenu(self.retranscribe_menu_button)
        retranscribe_menu.setStyleSheet(_MENU_STYLESHEET)
        raw_action = retranscribe_menu.addAction("Transcribe again (raw)")
        raw_action.setToolTip(
            "Re-transcribe without AI cleanup — keep the raw ASR text"
        )
        raw_action.triggered.connect(
            lambda: self._on_retranscribe(skip_cleanup=True)
        )
        self.retranscribe_menu_button.setMenu(retranscribe_menu)
        split_layout.addWidget(self.retranscribe_menu_button)

        self.retranscribe_split.setVisible(self._audio_path is not None)
        primary_actions.addWidget(self.retranscribe_split)
        primary_actions.addStretch()
        outer.addLayout(primary_actions)

        dismiss_actions = QHBoxLayout()
        dismiss_actions.setSpacing(8)
        dismiss_actions.addStretch()

        self.delete_button = DangerButton("Delete")
        self.delete_button.set_base_minimum_size(96, 40)
        self.delete_button.clicked.connect(self._on_delete)
        dismiss_actions.addWidget(self.delete_button)

        close_button = Button("Close")
        close_button.set_base_minimum_size(96, 40)
        close_button.clicked.connect(self.accept)
        dismiss_actions.addWidget(close_button)
        outer.addLayout(dismiss_actions)

    def _setup_shortcuts(self) -> None:
        """Bind Ctrl+C to copy the currently shown transcript."""
        shortcut = QShortcut(QKeySequence.StandardKey.Copy, self)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(self._copy_shown_text)

    def _build_metadata_section(self) -> Optional[QFrame]:
        """Build a fact grid for available timing/size metadata."""
        facts: list[tuple[str, str]] = []
        if self.entry.audio_duration is not None:
            facts.append(
                ("Audio duration", _format_seconds(self.entry.audio_duration))
            )
        if self.entry.transcription_time is not None:
            facts.append(
                ("Transcription time", _format_seconds(self.entry.transcription_time))
            )
        if self.entry.file_size is not None:
            facts.append(("File size", format_file_size(self.entry.file_size)))
        if self.entry.model:
            facts.append(("Model", self.entry.model))

        if not facts:
            return None

        frame = QFrame()
        frame.setObjectName("historyEntrySection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        heading = QLabel("Details")
        heading.setObjectName("historyEntrySectionTitle")
        layout.addWidget(heading)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        self.fact_labels: dict[str, QLabel] = {}
        for row, (caption, value) in enumerate(facts):
            caption_label = QLabel(caption)
            caption_label.setObjectName("historyEntryFactLabel")
            value_label = QLabel(value)
            value_label.setObjectName("historyEntryFactValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            grid.addWidget(caption_label, row, 0)
            grid.addWidget(value_label, row, 1)
            self.fact_labels[caption] = value_label

        layout.addLayout(grid)
        return frame

    def _on_version_toggled(self, checked: bool) -> None:
        """Swap Fixed/Raw transcript content when the segmented control changes."""
        if not checked:
            return
        show_raw = self.raw_btn.isChecked()
        self._showing_raw = show_raw
        if show_raw and self._raw_text is not None:
            self.transcript_text.setText(self._raw_text)
        else:
            self.transcript_text.setText(self._fixed_text)

    def _shown_text(self) -> str:
        """Return the currently displayed transcript version."""
        if self._showing_raw and self._raw_text is not None:
            return self._raw_text
        return self._fixed_text

    def _copy_shown_text(self) -> None:
        """Copy the currently shown transcript to the clipboard."""
        text = self._shown_text()
        try:
            QApplication.clipboard().setText(text)
            self.copied.emit()
            logger.info("Copied history entry transcript from dialog")
        except Exception as exc:
            logger.error("Failed to copy transcript from dialog: %s", exc)

    def _copy_raw_text(self) -> None:
        """Copy the raw ASR transcript to the clipboard."""
        if not self._raw_text:
            return
        try:
            QApplication.clipboard().setText(self._raw_text)
            self.copied.emit()
            logger.info("Copied raw history entry transcript from dialog")
        except Exception as exc:
            logger.error("Failed to copy raw transcript from dialog: %s", exc)

    def _on_retranscribe(self, skip_cleanup: bool = False) -> None:
        """Request re-transcription of the saved recording and close.

        Args:
            skip_cleanup: When True, skip the AI cleanup pass (raw ASR only).
        """
        if not self._audio_path:
            return
        self.retranscribe_requested.emit(self._audio_path, skip_cleanup)
        self.accept()

    def _on_delete(self) -> None:
        """Confirm deletion, then request delete and close."""
        reply = QMessageBox.question(
            self,
            "Delete Entry",
            "Delete this transcription from history?\n\n"
            "The saved recording file (if any) will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.delete_requested.emit(self.entry.id)
        self.accept()
