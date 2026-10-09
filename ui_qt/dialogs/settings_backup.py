"""Settings page for local backups and staged restore.

The application owns the coordinator and its workers.  This widget only
collects choices, confirms replacement, and renders coordinator signals.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QDate, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config import config
from services.format_utils import format_file_size
from ui_qt.dialogs.settings_fields import settings_caption
from ui_qt.utils.icons import design_icon
from ui_qt.widgets import Button, ElidingComboBox, InfoTile, PrimaryButton
from ui_qt.widgets.backup_calendar import BackupCalendar
from ui_qt.widgets.buttons import compact_primary_button, neutral_button
from ui_qt.widgets.eliding_label import ElidingLabel


def _field(info: Any, name: str, default=None):
    if isinstance(info, dict):
        return info.get(name, default)
    return getattr(info, name, default)


def _local_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        date = value
    else:
        try:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if date.tzinfo is not None:
        date = date.astimezone()
    return date


def _date_label(value: Any) -> str:
    date = _local_date(value)
    if date is None:
        return str(value) if value else "Unknown date"
    return f"{date.strftime('%b')} {date.day}, {date.year} at {date.strftime('%I:%M %p').lstrip('0')}"



def _action_row() -> QWidget:
    """A holder for a row of controls that never holds the page wider.

    Its width comes from the tile, so a narrow window stacks the controls
    (see ``_fits_side_by_side``) instead of growing a horizontal scroll bar.
    """
    row = QWidget()
    row.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return row


def _fits_side_by_side(row: QWidget, layout: QBoxLayout) -> bool:
    """Whether ``row`` is wide enough for its controls in one line."""
    widgets = [
        item.widget() for item in (layout.itemAt(i) for i in range(layout.count()))
        if item.widget() is not None and not item.widget().isHidden()
    ]
    needed = sum(max(w.sizeHint().width(), w.minimumWidth()) for w in widgets)
    needed += layout.spacing() * max(0, len(widgets) - 1)
    return row.width() >= needed

class BackupSettingsPage(QWidget):
    """Present backup state and forward work to an application coordinator.

    Expected coordinator methods: ``snapshot()``, ``create_backup(path,
    include_recordings)``, ``inspect_backup(path)``, ``prepare_restore(path)``,
    ``set_schedule(frequency, retention_count, destination)``, and ``cancel()``.
    Its Qt signals are ``busy_changed(bool)``, ``progress(phase, done, total)``,
    ``backup_finished(info, error)``, ``inspection_finished(info, error)``,
    ``restore_staged(staging_path, error)``, and ``schedule_changed()``.
    """

    operation_busy_changed = pyqtSignal(bool)

    def __init__(self, coordinator=None, parent=None):
        super().__init__(parent)
        self.setObjectName("backupSettingsPage")
        self._coordinator = coordinator
        self._inspected_path = ""
        self._inspected_info = None
        self._pending_inspection_path = ""
        self._busy = False
        self._operation = ""
        self._loading_schedule = False
        self._last_backup = None
        self._next_backup_at = None
        self._saved_frequency = "off"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        self.create_tile = InfoTile(
            "Back up local data",
            "Keep a local copy of your settings, transcripts, and meetings.",
            design_icon("box-blue.svg"),
        )
        self.create_tile.setProperty("backupCard", "create")
        self.last_backup_label = settings_caption("No backup created yet")
        self.last_backup_label.setObjectName("backupLastCreated")
        self.last_location_label = ElidingLabel("", mode=Qt.TextElideMode.ElideMiddle)
        self.last_location_label.setObjectName("backupLastLocation")
        self.last_size_label = settings_caption("")
        self.last_size_label.setObjectName("backupLastSize")
        # Nothing to say until a backup exists; start collapsed like refresh() does.
        self.last_location_label.hide()
        self.last_size_label.hide()
        self.include_recordings_check = QCheckBox("Include saved recordings")
        self.include_recordings_check.setObjectName("backupIncludeRecordings")
        self.include_recordings_check.setChecked(True)
        self.create_button = compact_primary_button(PrimaryButton("Create backup…"))
        self.create_button.setObjectName("backupCreateButton")
        self.create_button.clicked.connect(self._choose_backup_destination)
        self.create_tile.add_body(self.last_backup_label)
        self.create_tile.add_body(self.last_location_label)
        self.create_tile.add_body(self.last_size_label)
        self.create_row = _action_row()
        create_actions = self.create_actions = QBoxLayout(
            QBoxLayout.Direction.LeftToRight, self.create_row
        )
        create_actions.setContentsMargins(0, 0, 0, 0)
        create_actions.setSpacing(12)
        create_actions.addWidget(self.include_recordings_check)
        create_actions.addStretch()
        create_actions.addWidget(self.create_button)
        self.create_tile.add_body(self.create_row)
        self.create_tile.add_body(settings_caption(
            "Speech models, API keys, and paired devices are excluded."
        ))
        self.privacy_label = settings_caption(
            "Backup files are not encrypted. Keep them in a private location."
        )
        self.privacy_label.setObjectName("backupPrivacyNotice")
        self.create_tile.add_body(self.privacy_label)
        layout.addWidget(self.create_tile)

        self.restore_tile = InfoTile(
            "Inspect and restore",
            "Review a backup before restoring. Your current data is kept for recovery.",
            design_icon("refresh-blue.svg"),
        )
        self.restore_row = _action_row()
        restore_actions = self.restore_actions = QBoxLayout(
            QBoxLayout.Direction.LeftToRight, self.restore_row
        )
        restore_actions.setContentsMargins(0, 0, 0, 0)
        self.inspect_button = neutral_button(Button("Choose backup…"))
        self.inspect_button.setObjectName("backupInspectButton")
        self.inspect_button.clicked.connect(self._choose_restore_source)
        restore_actions.addWidget(self.inspect_button)
        self.restore_button = compact_primary_button(PrimaryButton("Restore and restart"))
        self.restore_button.setObjectName("backupRestoreButton")
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self._confirm_restore)
        restore_actions.addWidget(self.restore_button)
        restore_actions.addStretch()
        self.restore_tile.add_body(self.restore_row)
        self.inspection_label = settings_caption("No backup selected")
        self.inspection_label.setObjectName("backupInspectionSummary")
        self.inspection_label.setWordWrap(True)
        self.restore_tile.add_body(self.inspection_label)
        self.inspection_location_label = ElidingLabel("", mode=Qt.TextElideMode.ElideMiddle)
        self.inspection_location_label.hide()
        self.restore_tile.add_body(self.inspection_location_label)
        self.recovery_location_label = ElidingLabel("", mode=Qt.TextElideMode.ElideMiddle)
        self.recovery_location_label.setObjectName("backupRecoveryLocation")
        self.recovery_location_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.recovery_location_label.hide()
        self.restore_tile.add_body(self.recovery_location_label)

        self.schedule_tile = InfoTile(
            "Schedule",
            "Plan automatic backups and see the dates at a glance.",
            design_icon("layout-grid-blue.svg"),
        )
        self.schedule_tile.setProperty("backupCard", "schedule")
        schedule_body = QWidget()
        schedule_body.setObjectName("backupScheduleBody")
        self.schedule_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, schedule_body)
        self.schedule_layout.setContentsMargins(0, 0, 0, 0)
        self.schedule_layout.setSpacing(24)
        schedule_fields = QWidget()
        schedule_fields.setObjectName("backupScheduleFields")
        fields_layout = QVBoxLayout(schedule_fields)
        fields_layout.setContentsMargins(0, 0, 0, 0)
        fields_layout.setSpacing(12)
        self.schedule_summary_label = settings_caption("Automatic backups are off")
        self.schedule_summary_label.setObjectName("backupScheduleSummary")
        fields_layout.addWidget(self.schedule_summary_label)
        self.frequency_combo = ElidingComboBox()
        self.frequency_combo.setObjectName("backupFrequencyCombo")
        self.frequency_combo.addItem("Off", "off")
        self.frequency_combo.addItem("Daily", "daily")
        self.frequency_combo.addItem("Weekly", "weekly")
        self.frequency_combo.currentIndexChanged.connect(self._update_schedule_controls)
        fields_layout.addWidget(self._field_row("Frequency", self.frequency_combo))

        self.destination_edit = QLineEdit()
        self.destination_edit.setObjectName("backupScheduleDestination")
        self.destination_edit.setReadOnly(True)
        self.destination_edit.setPlaceholderText("Choose a local folder")
        self.destination_button = neutral_button(Button("Browse…"))
        self.destination_button.setObjectName("backupScheduleBrowseButton")
        self.destination_button.clicked.connect(self._choose_schedule_destination)
        destination_row = QWidget()
        destination_row.setObjectName("backupDestinationRow")
        destination_layout = QHBoxLayout(destination_row)
        destination_layout.setContentsMargins(0, 0, 0, 0)
        destination_layout.addWidget(self.destination_edit, stretch=1)
        destination_layout.addWidget(self.destination_button)
        fields_layout.addWidget(self._field_row("Save to", destination_row))

        self.retention_spin = QSpinBox()
        self.retention_spin.setObjectName("backupRetentionCount")
        self.retention_spin.setRange(1, 100)
        self.retention_spin.setValue(5)
        self.retention_spin.setSuffix(" backups")
        self.retention_spin.setKeyboardTracking(False)
        fields_layout.addWidget(self._field_row("Keep latest", self.retention_spin))
        self.save_schedule_button = neutral_button(Button("Save schedule"))
        self.save_schedule_button.setObjectName("backupSaveScheduleButton")
        self.save_schedule_button.clicked.connect(self._save_schedule)
        fields_layout.addWidget(self.save_schedule_button, alignment=Qt.AlignmentFlag.AlignLeft)
        fields_layout.addWidget(settings_caption(
            "Runs while OpenWhisper is open and idle, with saved recordings included. "
            "Older automatic backups are removed after the limit is reached. "
            "Planned dates follow your saved schedule and may shift while the app is closed or busy."
        ))
        fields_layout.addStretch()
        self.calendar = BackupCalendar()
        self.schedule_layout.addWidget(schedule_fields, stretch=1)
        self.schedule_layout.addWidget(self.calendar, stretch=1)
        self.schedule_tile.add_body(schedule_body)
        layout.addWidget(self.schedule_tile)
        layout.addWidget(self.restore_tile)

        for tile in (self.create_tile, self.schedule_tile, self.restore_tile):
            tile.set_body_indented(False)
            tile.layout().setContentsMargins(20, 18, 20, 18)
            tile._body_layout.setSpacing(10)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("backupProgressBar")
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.cancel_button = neutral_button(Button("Cancel"))
        self.cancel_button.setObjectName("backupCancelButton")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.hide()
        layout.addWidget(self.cancel_button)
        self.status_label = settings_caption("")
        self.status_label.setObjectName("backupStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Backup status")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        layout.addStretch()

        self._connect_coordinator()
        self.refresh()

    @staticmethod
    def _field_row(caption: str, control: QWidget) -> QWidget:
        row = QWidget()
        row.setObjectName("backupFieldGroup")
        line = QVBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(5)
        label = QLabel(caption)
        label.setObjectName("settingsTileFieldLabel")
        label.setBuddy(control)
        line.addWidget(label)
        line.addWidget(control, stretch=1)
        return row

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        narrow = self.width() < 680
        self.schedule_layout.setDirection(
            QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        )
        # Both rows stack together once either can't hold its controls side
        # by side, measured in the current font rather than at a set width.
        stacked = not (
            _fits_side_by_side(self.create_row, self.create_actions)
            and _fits_side_by_side(self.restore_row, self.restore_actions)
        )
        action_direction = (
            QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
        )
        self.create_actions.setDirection(action_direction)
        self.restore_actions.setDirection(action_direction)
        self.create_actions.setAlignment(
            self.create_button,
            Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag.AlignRight,
        )
        for button in (self.inspect_button, self.restore_button):
            self.restore_actions.setAlignment(button, Qt.AlignmentFlag.AlignLeft)

    def _update_calendar(self) -> None:
        latest = _local_date(_field(self._last_backup, "created_at"))
        latest_date = QDate(latest.year, latest.month, latest.day) if latest else QDate()
        next_due = QDate()
        interval = {"daily": 1, "weekly": 7}.get(self._saved_frequency, 0)
        due = None
        if interval and self._next_backup_at is not None:
            try:
                due = datetime.fromtimestamp(self._next_backup_at).astimezone()
                next_due = QDate(due.year, due.month, due.day)
            except (ValueError, TypeError, OverflowError, OSError):
                pass
        self.calendar.set_dates(latest_date, next_due, interval)
        if not interval:
            summary = "Automatic backups are off"
        elif due is None:
            summary = f"{self._saved_frequency.capitalize()} backups enabled"
        else:
            summary = (
                "Next backup: when the app is idle" if next_due <= QDate.currentDate()
                else f"Next backup: {_date_label(due)}"
            )
        self.schedule_summary_label.setText(summary)

    def _connect_coordinator(self) -> None:
        if self._coordinator is None:
            return
        for signal_name, callback in (
            ("busy_changed", self.set_busy),
            ("progress", self.set_progress),
            ("backup_finished", self.show_backup_result),
            ("inspection_finished", self.show_inspection_result),
            ("restore_staged", self.show_restore_result),
            ("schedule_changed", self.refresh),
        ):
            signal = getattr(self._coordinator, signal_name, None)
            if signal is not None:
                signal.connect(callback)

    def refresh(self) -> None:
        """Render the coordinator's latest persisted status and schedule."""
        if self._coordinator is None:
            self.status_label.setText("Backup and restore are unavailable in this session.")
            self._update_actions()
            return
        try:
            state = self._coordinator.snapshot() or {}
        except Exception as exc:
            self.status_label.setText(f"Could not load backup settings: {exc}")
            self._update_actions()
            return
        info = state.get("last_backup")
        self._show_last_backup(info)
        previous_data_dir = str(state.get("previous_data_dir") or "")
        self.recovery_location_label.setText(
            f"Previous data: {previous_data_dir}"
            if previous_data_dir else ""
        )
        self.recovery_location_label.setVisible(bool(previous_data_dir))
        self._loading_schedule = True
        try:
            frequency = state.get("frequency", "off")
            self._saved_frequency = frequency
            self._next_backup_at = state.get("next_backup_at")
            index = self.frequency_combo.findData(frequency)
            self.frequency_combo.setCurrentIndex(max(0, index))
            self.retention_spin.setValue(int(state.get("retention_count", 5)))
            self.destination_edit.setText(str(state.get("destination") or ""))
        finally:
            self._loading_schedule = False
        self._busy = bool(state.get("busy", self._busy))
        self._operation = str(state.get("operation") or "")
        self._update_schedule_controls()
        self._update_actions()
        self._update_calendar()
        if state.get("last_error"):
            self.status_label.setText(str(state["last_error"]))

    def _show_last_backup(self, info: Any) -> None:
        self._last_backup = info
        self.last_location_label.setVisible(info is not None)
        self.last_size_label.setVisible(info is not None)
        if info is None:
            self.last_backup_label.setText("No backup created yet")
            self.last_location_label.setText("")
            self.last_size_label.clear()
            return
        self.last_backup_label.setText(f"Last backup: {_date_label(_field(info, 'created_at'))}")
        self.last_location_label.setText(f"Location: {_field(info, 'path', '')}")
        self.last_size_label.setText(
            f"Size: {format_file_size(_field(info, 'archive_bytes', 0))}"
        )
        self._update_calendar()

    def _update_schedule_controls(self, *_args) -> None:
        enabled = self.frequency_combo.currentData() != "off"
        available = self._coordinator is not None and not self._busy
        self.destination_edit.setEnabled(enabled and available)
        self.destination_button.setEnabled(enabled and available)
        self.retention_spin.setEnabled(enabled and available)

    def _update_actions(self) -> None:
        available = self._coordinator is not None and not self._busy
        self.create_button.setEnabled(available)
        self.include_recordings_check.setEnabled(available)
        self.inspect_button.setEnabled(available)
        self.restore_button.setEnabled(available and bool(self._inspected_path))
        self.frequency_combo.setEnabled(available)
        self.save_schedule_button.setEnabled(available)
        self._update_schedule_controls()
        cancelable = self._busy and self._operation in ("backup", "Creating backup")
        self.cancel_button.setVisible(cancelable)
        self.cancel_button.setEnabled(cancelable and self._coordinator is not None)
        self.progress_bar.setVisible(self._busy)

    def _choose_backup_destination(self) -> None:
        if self._coordinator is None or self._busy:
            return
        folder = self.destination_edit.text().strip()
        default = str(Path(folder) / "OpenWhisper-backup.owbackup") if folder else "OpenWhisper-backup.owbackup"
        path, _ = QFileDialog.getSaveFileName(
            self, "Create OpenWhisper backup", default,
            "OpenWhisper backups (*.owbackup);;All files (*)",
        )
        if path:
            self._start("Creating backup", lambda: self._coordinator.create_backup(
                path, self.include_recordings_check.isChecked()
            ))

    def _choose_restore_source(self) -> None:
        if self._coordinator is None or self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Inspect OpenWhisper backup", self.destination_edit.text().strip(),
            "OpenWhisper backups (*.owbackup *.zip);;All files (*)",
        )
        if not path:
            return
        self._inspected_path = ""
        self._inspected_info = None
        self._pending_inspection_path = path
        self.inspection_location_label.setText("")
        self.inspection_location_label.hide()
        self.inspection_label.setText("Inspecting backup…")
        self._update_actions()
        self._start("Inspecting backup", lambda: self._coordinator.inspect_backup(path))

    def _confirm_restore(self) -> None:
        if self._coordinator is None or self._busy or not self._inspected_path:
            return
        response = QMessageBox.question(
            self,
            "Restore local data",
            "Restore this backup and restart OpenWhisper?\n\n"
            "This replaces local settings, transcription history, meeting data, and "
            "recordings with the contents shown above. Your current data is kept "
            "for recovery. API keys and paired devices are excluded.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response == QMessageBox.StandardButton.Yes:
            path = self._inspected_path
            self._start("Preparing restore", lambda: self._coordinator.prepare_restore(path))

    def _choose_schedule_destination(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose automatic backup folder", self.destination_edit.text().strip()
        )
        if folder:
            self.destination_edit.setText(folder)

    def _save_schedule(self) -> None:
        if self._coordinator is None or self._busy:
            return
        frequency = self.frequency_combo.currentData()
        destination = self.destination_edit.text().strip()
        if frequency != "off" and not destination:
            self.status_label.setText("Choose a destination folder for automatic backups.")
            return
        if frequency != "off":
            try:
                folder = Path(destination).resolve()
                if not folder.is_dir():
                    self.status_label.setText("The automatic backup destination must be an existing folder.")
                    return
                owned_folders = (Path(config.RECORDINGS_FOLDER).resolve(),
                                 Path(config.MEETINGS_FOLDER).resolve())
            except OSError as exc:
                self.status_label.setText(f"Could not check backup destination: {exc}")
                return
            if any(folder.is_relative_to(owned) for owned in owned_folders):
                self.status_label.setText(
                    "Choose a folder outside OpenWhisper's recordings and meetings folders."
                )
                return
        try:
            self._coordinator.set_schedule(frequency, self.retention_spin.value(), destination)
        except Exception as exc:
            self.status_label.setText(f"Could not save automatic backup settings: {exc}")
            return
        self.status_label.setText(
            "Automatic backups are off." if frequency == "off" else
            f"Automatic backups set to {frequency}."
        )

    def _start(self, operation: str, request) -> None:
        self._operation = operation
        self.set_busy(True)
        self.status_label.setText(f"{operation}…")
        try:
            request()
        except Exception as exc:
            self.set_busy(False)
            self.status_label.setText(f"{operation} failed: {exc}")

    def _cancel(self) -> None:
        if self._coordinator is None:
            return
        try:
            self._coordinator.cancel()
        except Exception as exc:
            self.status_label.setText(f"Could not cancel: {exc}")
            return
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling…")

    def set_busy(self, busy: bool) -> None:
        changed = self._busy != bool(busy)
        self._busy = bool(busy)
        if busy:
            self._operation = getattr(self._coordinator, "operation", "") or self._operation
            self.progress_bar.setRange(0, 0)
        self._update_actions()
        if changed:
            self.operation_busy_changed.emit(self._busy)

    def set_progress(self, phase: str, done: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(max(0, min(100, round(100 * done / total))))
            self.status_label.setText(f"{phase}: {self.progress_bar.value()}%")
        else:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(str(phase))

    def show_backup_result(self, info: Any, error: str = "") -> None:
        self.set_busy(False)
        if info is None:
            self.status_label.setText(f"Backup failed: {error or 'Unknown error'}")
            return
        self._show_last_backup(info)
        warnings = _field(info, "warnings", ()) or ()
        self.status_label.setText(
            (f"Backup saved at {_field(info, 'path', '')}\n{error}"
             if error else f"Backup created: {_field(info, 'path', '')}")
            + ("\nWarnings: " + "; ".join(str(item) for item in warnings) if warnings else "")
        )

    def show_inspection_result(self, info: Any, error: str = "") -> None:
        self.set_busy(False)
        if error or info is None:
            self._inspected_path = ""
            self._inspected_info = None
            self.inspection_location_label.setText("")
            self.inspection_location_label.hide()
            self.inspection_label.setText(f"Could not inspect backup: {error or 'Unknown error'}")
            self.status_label.setText(self.inspection_label.text())
            self._update_actions()
            return
        self._inspected_info = info
        self._inspected_path = str(_field(info, "path", "") or self._pending_inspection_path)
        recordings = "included" if _field(info, "include_recordings", False) else "not included"
        warnings = _field(info, "warnings", ()) or ()
        self.inspection_label.setText(
            f"Backup from {_date_label(_field(info, 'created_at'))}\n"
            f"Version {_field(info, 'app_version', '?')} · "
            f"{_field(info, 'file_count', 0)} files · "
            f"{format_file_size(_field(info, 'archive_bytes', 0))} archive\n"
            "Contents: app settings, transcription history, and saved meetings (if any).\n"
            f"Saved recordings: {recordings} · "
            f"{format_file_size(_field(info, 'total_bytes', 0))} before compression"
            + ("\nWarnings: " + "; ".join(str(item) for item in warnings) if warnings else "")
        )
        self.inspection_location_label.setText(f"Location: {self._inspected_path}")
        self.inspection_location_label.show()
        self.status_label.setText("Backup inspected. Review the contents before restoring.")
        self._update_actions()

    def show_restore_result(self, staging_path: str, error: str = "") -> None:
        self.set_busy(False)
        if error or not staging_path:
            self.status_label.setText(f"Restore could not be prepared: {error or 'Unknown error'}")
            return
        self.status_label.setText("Restore staged. OpenWhisper will restart to apply it.")
