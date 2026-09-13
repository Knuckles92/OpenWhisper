import logging
from pathlib import Path

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFrame, QLabel
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon

from config import bundle_root

from ui_qt.widgets.cards import ControlPanel
from ui_qt.widgets.buttons import SuccessButton, DangerButton, WarningButton
from ui_qt.widgets.transcription_tab_base import TranscriptionTabBase
from ui_qt.widgets.wrapped_label import WrappedLabel
from ui_qt.widgets.engine_field import engine_combo
from services.cleanup_profiles import load_cleanup_profiles
from services.hotkey_manager import format_hotkey_display
from services.settings import SettingsKey, settings_manager

logger = logging.getLogger(__name__)


class QuickRecordTab(TranscriptionTabBase):
    record_toggled = pyqtSignal(bool)
    record_canceled = pyqtSignal()
    copy_requested = pyqtSignal(str)
    profiles_requested = pyqtSignal()

    CONTENT_OBJECT_NAME = "quickRecordContent"
    INITIAL_STATUS = "Ready to record"
    TRANSCRIPT_PLACEHOLDER = (
        "Transcription will appear here...\n"
        "Start recording to begin."
    )

    def __init__(self, parent=None):
        super().__init__(parent)

        # State (safe to set after the base constructor: _setup_ui never
        # reads it, and no signals can fire during init)
        self.is_recording = False

        self._partial_buffer = []

        icons = Path(bundle_root()) / "ui_qt" / "assets" / "tabler"
        self._copy_icon = QIcon(str(icons / "copy-gray.svg"))
        self._copied_icon = QIcon(str(icons / "check-green.svg"))
        self.copy_button = QPushButton()
        self.collapsed_copy_button = QPushButton("Copy")
        for button in (self.copy_button, self.collapsed_copy_button):
            button.setObjectName("transcriptCopyButton")
            button.setIconSize(QSize(16, 16))
            button.setFixedHeight(26)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName("Copy transcript")
            button.clicked.connect(self._request_copy)
        self.copy_button.setFixedWidth(26)
        self.transcript_pane.set_corner_widget(self.copy_button)
        self.transcription_card.header_layout.insertWidget(
            2, self.collapsed_copy_button
        )
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(1500)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_feedback)
        self.transcript_text.textChanged.connect(self._sync_copy_actions)
        self.transcription_card.toggled.connect(self._sync_copy_actions)
        self._sync_copy_actions()

    def set_transcription_collapsed(self, collapsed: bool):
        super().set_transcription_collapsed(collapsed)
        if hasattr(self, "copy_button"):
            self._sync_copy_actions()

    def _sync_copy_actions(self):
        self._copy_feedback_timer.stop()
        self._reset_copy_feedback()
        has_text = bool(self.shown_transcript().strip())
        self.copy_button.setEnabled(has_text)
        self.collapsed_copy_button.setVisible(
            has_text and self.is_transcription_collapsed()
        )
        if self.is_transcription_collapsed():
            self.transcription_card.setMaximumHeight(
                self.transcription_card.sizeHint().height()
            )

    def _request_copy(self):
        text = self.shown_transcript()
        if text.strip():
            self.copy_requested.emit(text)

    def show_copy_result(self, succeeded: bool):
        for button in (self.copy_button, self.collapsed_copy_button):
            button.setIcon(self._copied_icon if succeeded else self._copy_icon)
            button.setToolTip(
                "Copied to clipboard" if succeeded else "Copy failed. Try again."
            )
        self.collapsed_copy_button.setText("Copied" if succeeded else "Retry copy")
        self._copy_feedback_timer.start()

    def _reset_copy_feedback(self):
        for button in (self.copy_button, self.collapsed_copy_button):
            button.setIcon(self._copy_icon)
            button.setToolTip("Copy transcript")
        self.collapsed_copy_button.setText("Copy")

    def _build_content_before_status(self, layout: QVBoxLayout):
        card = QFrame()
        card.setObjectName("engineCard")
        body = QVBoxLayout(card)
        body.setContentsMargins(14, 10, 14, 10)
        body.setSpacing(6)
        row = QHBoxLayout()
        label = QLabel("Cleanup profile")
        label.setObjectName("engineResolvedLabel")
        self.profile_combo = engine_combo([])
        self.profile_combo.setAccessibleName("Quick Record cleanup profile")
        label.setBuddy(self.profile_combo)
        row.addWidget(label)
        row.addWidget(self.profile_combo, 1)
        self.manage_profiles_button = QPushButton("Manage…")
        self.manage_profiles_button.setToolTip("Create cleanup profiles and assign recording shortcuts")
        self.manage_profiles_button.clicked.connect(self.profiles_requested)
        row.addWidget(self.manage_profiles_button)
        body.addLayout(row)
        self.profile_hint = WrappedLabel()
        self.profile_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.profile_hint.setObjectName("infoLabel")
        body.addWidget(self.profile_hint)
        layout.addWidget(card)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self.refresh_cleanup_profiles()

    def selected_cleanup_profile_id(self) -> str:
        return self.profile_combo.currentData() or ""

    def refresh_cleanup_profiles(self) -> None:
        settings = settings_manager.load_all_settings()
        selected = settings.get(SettingsKey.QUICK_RECORD_PROFILE, "")
        recording = getattr(self, "is_recording", False)
        active_profile = getattr(self, "_active_cleanup_profile", None)
        if recording:
            selected = active_profile.id if active_profile else ""
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("Standard dictation", "")
        self._cleanup_profiles = load_cleanup_profiles(settings)
        if recording and active_profile:
            self._cleanup_profiles = [p for p in self._cleanup_profiles if p.id != active_profile.id]
            self._cleanup_profiles.append(active_profile)
        for profile in self._cleanup_profiles:
            self.profile_combo.addItem(profile.name, profile.id)
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(selected)))
        self.profile_combo.blockSignals(False)
        self._refresh_profile_hint()

    def set_recording_profile(self, profile) -> None:
        self._active_cleanup_profile = profile

    def _on_profile_changed(self) -> None:
        settings_manager.save_setting(SettingsKey.QUICK_RECORD_PROFILE, self.selected_cleanup_profile_id())
        self._refresh_profile_hint()

    def _refresh_profile_hint(self) -> None:
        selected = self.selected_cleanup_profile_id()
        profile = next((p for p in self._cleanup_profiles if p.id == selected), None)
        if profile:
            shortcut = format_hotkey_display(profile.hotkey)
            self.profile_hint.setText(
                "Always uses AI cleanup · " + (f"Record with {shortcut}" if shortcut else "Click Start Recording")
            )
            self.profile_combo.setToolTip(profile.instructions)
        else:
            self.profile_hint.setText("Uses your usual cleanup settings and standard recording shortcut.")
            self.profile_combo.setToolTip("Standard dictation")
        if getattr(self, "is_recording", False):
            self.profile_hint.setText(f"Recording with {profile.name if profile else 'Standard dictation'}")
        self.load_cleanup_setting()
        if hasattr(self, "record_button"):
            shortcut = format_hotkey_display(profile.hotkey) if profile else getattr(self, "_standard_record_key", "")
            self.record_button.set_hotkey(shortcut)
            self.stop_button.set_hotkey(shortcut)

    def load_cleanup_setting(self):
        super().load_cleanup_setting()
        selected = hasattr(self, "profile_combo") and bool(self.selected_cleanup_profile_id())
        if selected:
            self.cleanup_check.blockSignals(True)
            self.cleanup_check.setChecked(True)
            self.cleanup_check.blockSignals(False)
        self.cleanup_check.setEnabled(not selected)

    def _build_content_after_status(self, layout: QVBoxLayout):
        control_panel = ControlPanel()
        # The content layout supplies vertical separation between the profile
        # card, buttons, and transcript; keep the minimum-height window usable.
        control_panel.layout.setContentsMargins(16, 0, 16, 0)
        control_panel.layout.setSpacing(12)

        self.record_button = SuccessButton("Start Recording")
        self.cancel_button = WarningButton("Cancel")
        self.cancel_button.set_active(False)
        self.stop_button = DangerButton("Stop")
        self.stop_button.set_active(False)

        buttons_widget = QWidget()
        buttons_layout = QVBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(12)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        top_row.addWidget(self.record_button, stretch=1)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)
        bottom_row.addWidget(self.stop_button, stretch=1)
        bottom_row.addWidget(self.cancel_button, stretch=1)

        buttons_layout.addLayout(top_row)
        buttons_layout.addLayout(bottom_row)
        buttons_widget.setMaximumWidth(420)

        control_panel.layout.addStretch()
        control_panel.layout.addWidget(buttons_widget)
        control_panel.layout.addStretch()

        layout.addWidget(control_panel)

    def _connect_signals(self):
        super()._connect_signals()
        self.record_button.clicked.connect(self._on_record_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)

    def _on_record_clicked(self):
        """Request a start; Recording chrome waits for a successful stream."""
        self.record_button.set_active(False)
        self.record_toggled.emit(True)

    def _on_stop_clicked(self):
        """Request a stop; button state waits for recording_state_changed."""
        self.record_toggled.emit(False)

    def _on_cancel_clicked(self):
        """Request a cancel; chrome is reset by the cancel path."""
        self.record_canceled.emit()

    def _update_recording_state(self):
        self.refresh_cleanup_profiles()
        self.profile_combo.setEnabled(not self.is_recording)
        if self.is_recording:
            self.record_button.set_active(False)
            self.record_button.setText("Recording...")
            self.stop_button.set_active(True)
            self.cancel_button.set_active(True)
            self.set_backend_enabled(False)
            self.local_engine.set_busy(True)
            # The streaming runtime refuses to reconfigure mid-recording, so
            # the toggle would persist without taking effect until the next
            # recording. Lock it with the backend choice instead.
            self.live_preview_check.setEnabled(False)
            self.set_status("Recording in progress...")
        else:
            self.record_button.set_active(True)
            self.record_button.setText("Start Recording")
            self.stop_button.set_active(False)
            self.cancel_button.set_active(False)
            self.set_backend_enabled(True)
            self.local_engine.set_busy(False)
            self.live_preview_check.setEnabled(True)
            self.set_status("Ready to record")

    def append_transcription(self, text: str):
        self.set_transcript(self.shown_transcript() + text)
        cursor = self.transcript_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcript_text.setTextCursor(cursor)

    def set_partial_transcription(self, text: str, is_final: bool):
        if is_final:
            # Incremental preview emits the full accumulated preview each cycle,
            # so we REPLACE (not append) the buffer contents.
            self._partial_buffer = [text] if text else []

        combined = " ".join(self._partial_buffer)
        if not is_final:
            if combined:
                combined += " "
            combined += text + " ..."

        self.set_transcript(combined)

        cursor = self.transcript_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcript_text.setTextCursor(cursor)

    def clear_partial_transcription(self):
        self._partial_buffer.clear()

    def update_hotkeys(self, record_key: str, cancel_key: str, enable_disable_key: str = ""):
        self._standard_record_key = record_key
        self._refresh_profile_hint()
        self.cancel_button.set_hotkey(cancel_key)
