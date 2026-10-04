"""Backup settings UI uses a fake coordinator and never touches user data."""

import os
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QDate, QObject, QPoint, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QBoxLayout, QMessageBox

from services.settings import SettingsManager
from ui_qt.dialogs import settings_dialog as dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_backup import BackupSettingsPage
from ui_qt.dialogs.settings_destinations import BACKUP
from ui_qt.utils.palette import current_palette, set_current_palette
from ui_qt.utils.theme_manager import ThemeManager


class FakeCoordinator(QObject):
    busy_changed = pyqtSignal(bool)
    progress = pyqtSignal(str, int, int)
    backup_finished = pyqtSignal(object, str)
    inspection_finished = pyqtSignal(object, str)
    restore_staged = pyqtSignal(str, str)
    schedule_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.calls = []
        self.state = {
            "last_backup": None,
            "frequency": "off",
            "retention_count": 7,
            "destination": "",
            "busy": False,
        }

    def snapshot(self):
        return dict(self.state)

    def create_backup(self, path, include_recordings):
        self.calls.append(("create", path, include_recordings))

    def inspect_backup(self, path):
        self.calls.append(("inspect", path))

    def prepare_restore(self, path):
        self.calls.append(("restore", path))

    def set_schedule(self, frequency, retention_count, destination):
        self.calls.append(("schedule", frequency, retention_count, destination))
        self.state.update(frequency=frequency, retention_count=retention_count,
                          destination=destination)
        self.schedule_changed.emit()

    def cancel(self):
        self.calls.append(("cancel",))


def _info(path: str):
    return SimpleNamespace(
        path=Path(path), created_at="2026-10-04T12:30:00",
        app_version="2.5.0", schema_version=1, include_recordings=True,
        file_count=4, total_bytes=4096, archive_bytes=2048,
    )


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


def test_manual_backup_uses_selected_path_and_reports_progress(monkeypatch):
    coordinator = FakeCoordinator()
    page = BackupSettingsPage(coordinator)
    assert page.include_recordings_check.isChecked()
    assert "not encrypted" in page.privacy_label.text()
    monkeypatch.setattr("ui_qt.dialogs.settings_backup.QFileDialog.getSaveFileName",
                        lambda *_args: ("/tmp/manual.owbackup", ""))

    page.include_recordings_check.setChecked(False)
    page.create_button.click()
    assert coordinator.calls == [("create", "/tmp/manual.owbackup", False)]
    assert not page.create_button.isEnabled()
    assert not page.cancel_button.isHidden()
    page.cancel_button.click()
    assert coordinator.calls[-1] == ("cancel",)
    coordinator.progress.emit("Saving history", 1, 2)
    assert "50%" in page.status_label.text()
    coordinator.backup_finished.emit(_info("/tmp/manual.owbackup"), "")
    assert page.create_button.isEnabled()
    assert "manual.owbackup" in page.last_location_label.text()
    assert "Last backup" in page.last_backup_label.text()
    coordinator.backup_finished.emit(_info("/tmp/manual.owbackup"),
                                     "retention could not be updated")
    assert "Backup saved" in page.status_label.text()
    assert "retention could not be updated" in page.status_label.text()


def test_without_coordinator_controls_are_unavailable():
    page = BackupSettingsPage()
    assert not page.create_button.isEnabled()
    assert not page.inspect_button.isEnabled()
    assert not page.save_schedule_button.isEnabled()
    assert "unavailable" in page.status_label.text()


def test_previous_restore_data_path_is_visible_and_selectable():
    coordinator = FakeCoordinator()
    coordinator.state["previous_data_dir"] = "/tmp/openwhisper/previous-123"
    page = BackupSettingsPage(coordinator)
    assert "previous-123" in page.recovery_location_label.text()
    assert not page.recovery_location_label.isHidden()
    assert page.recovery_location_label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse


def test_restore_requires_inspection_and_explicit_confirmation(monkeypatch):
    coordinator = FakeCoordinator()
    page = BackupSettingsPage(coordinator)
    assert not page.restore_button.isEnabled()
    monkeypatch.setattr("ui_qt.dialogs.settings_backup.QFileDialog.getOpenFileName",
                        lambda *_args: ("/tmp/recover.owbackup", ""))

    page.inspect_button.click()
    assert coordinator.calls == [("inspect", "/tmp/recover.owbackup")]
    assert not page.restore_button.isEnabled()
    info = _info("/tmp/recover.owbackup")
    info.warnings = ("One saved recording was missing",)
    coordinator.inspection_finished.emit(info, "")
    assert page.restore_button.isEnabled()
    assert "4 files" in page.inspection_label.text()
    assert "recordings: included" in page.inspection_label.text().lower()
    assert "One saved recording was missing" in page.inspection_label.text()

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    page.restore_button.click()
    assert not any(call[0] == "restore" for call in coordinator.calls)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    page.restore_button.click()
    assert coordinator.calls[-1] == ("restore", str(Path("/tmp/recover.owbackup")))
    assert page.cancel_button.isHidden()
    coordinator.restore_staged.emit("/tmp/staged", "")
    assert "restart" in page.status_label.text().lower()

    page.inspect_button.click()
    coordinator.inspection_finished.emit(None, "Bad archive")
    assert not page.restore_button.isEnabled()
    assert "Bad archive" in page.inspection_label.text()


def test_automatic_backup_is_off_until_explicitly_saved(monkeypatch, tmp_path):
    coordinator = FakeCoordinator()
    page = BackupSettingsPage(coordinator)
    assert page.frequency_combo.currentData() == "off"
    assert not coordinator.calls

    page.frequency_combo.setCurrentIndex(page.frequency_combo.findData("daily"))
    page.save_schedule_button.click()
    assert not coordinator.calls
    assert "destination folder" in page.status_label.text()

    folder = tmp_path / "backups"
    folder.mkdir()
    monkeypatch.setattr("ui_qt.dialogs.settings_backup.QFileDialog.getExistingDirectory",
                        lambda *_args: str(folder))
    page.destination_button.click()
    page.retention_spin.setValue(5)
    assert not coordinator.calls
    page.save_schedule_button.click()
    assert coordinator.calls == [("schedule", "daily", 5, str(folder))]

    page.frequency_combo.setCurrentIndex(page.frequency_combo.findData("off"))
    page.save_schedule_button.click()
    assert coordinator.calls[-1] == ("schedule", "off", 5, str(folder))


def test_schedule_rejects_missing_and_recording_destinations(monkeypatch, tmp_path):
    coordinator = FakeCoordinator()
    page = BackupSettingsPage(coordinator)
    page.frequency_combo.setCurrentIndex(page.frequency_combo.findData("weekly"))
    page.destination_edit.setText(str(tmp_path / "missing"))
    page.save_schedule_button.click()
    assert not coordinator.calls
    assert "existing folder" in page.status_label.text()

    recordings = tmp_path / "recordings"
    nested = recordings / "backups"
    nested.mkdir(parents=True)
    monkeypatch.setattr("ui_qt.dialogs.settings_backup.config.RECORDINGS_FOLDER", str(recordings))
    page.destination_edit.setText(str(nested))
    page.save_schedule_button.click()
    assert not coordinator.calls
    assert "outside" in page.status_label.text()


def test_calendar_shows_saved_schedule_and_latest_backup_only():
    coordinator = FakeCoordinator()
    coordinator.state.update(
        last_backup=_info("/tmp/manual.owbackup"), frequency="weekly",
        next_backup_at=datetime(2026, 10, 11, 12, tzinfo=timezone.utc).timestamp(),
    )
    page = BackupSettingsPage(coordinator)
    calendar = page.calendar
    due = datetime.fromtimestamp(coordinator.state["next_backup_at"])
    due_date = QDate(due.year, due.month, due.day)
    assert calendar.grid.latest == QDate(2026, 10, 4)
    assert calendar.grid.is_planned(due_date)
    assert calendar.grid.is_planned(due_date.addDays(7))
    assert not calendar.grid.is_planned(due_date.addDays(-7))
    assert not calendar.grid.is_planned(due_date.addDays(1))

    page.frequency_combo.setCurrentIndex(page.frequency_combo.findData("daily"))
    assert not calendar.grid.is_planned(due_date.addDays(1))
    calendar.grid.setSelectedDate(due_date)
    assert "Automatic backup planned" in calendar.detail_label.text()
    calendar.grid.setSelectedDate(QDate(2026, 10, 4))
    assert "Latest backup saved" in calendar.detail_label.text()
    calendar.next_button.click()
    assert calendar.grid.monthShown() == 11
    calendar.today_button.click()
    assert calendar.grid.selectedDate() == QDate.currentDate()
    assert calendar.grid.monthShown() == QDate.currentDate().month()
    assert not coordinator.calls

    coordinator.state.update(frequency="off", next_backup_at=None)
    coordinator.schedule_changed.emit()
    assert not calendar.grid.is_planned(due_date)
    assert "off" in page.schedule_summary_label.text()
    coordinator.backup_finished.emit(_info("/tmp/new.owbackup"), "")
    assert calendar.grid.latest == QDate(2026, 10, 4)


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("ui_mode", ["classic", "omarchy"])
def test_backup_card_text_has_no_opaque_background_and_controls_fit(
    monkeypatch, theme, ui_mode,
):
    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style = app.styleSheet()
    previous_palette = current_palette()
    app.setStyleSheet(ThemeManager(theme).stylesheet)
    coordinator = FakeCoordinator()
    coordinator.state["last_backup"] = _info("/tmp/manual.owbackup")
    page = BackupSettingsPage(coordinator)
    try:
        page.resize(860, 1000)
        page.show()
        for _ in range(4):
            app.processEvents()
        picture = page.grab().toImage()
        for label in (page.last_backup_label, page.last_location_label, page.last_size_label):
            point = label.mapTo(page, QPoint(label.width() - 2, 2))
            assert picture.pixelColor(point) == current_palette().color("slate-surface")

        page.resize(360, 1500)
        for _ in range(4):
            app.processEvents()
        page.resize(360, 1500)
        app.processEvents()
        assert page.width() == 360
        assert page.schedule_layout.direction() == QBoxLayout.Direction.TopToBottom
        for control in (page.create_button, page.inspect_button, page.restore_button,
                        page.save_schedule_button, page.destination_edit, page.calendar):
            right = control.mapTo(page, QPoint(control.width(), 0)).x()
            assert right <= page.width()
    finally:
        page.close()
        app.setStyleSheet(previous_style)
        set_current_palette(previous_palette)


@pytest.mark.parametrize("ui_mode", ["classic", "omarchy"])
def test_backup_page_is_lazy_searchable_and_usable_at_narrow_size(
    tmp_path, monkeypatch, ui_mode,
):
    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style = app.styleSheet()
    app.setStyleSheet(ThemeManager().stylesheet)
    coordinator = FakeCoordinator()
    store = SettingsManager(str(tmp_path / "settings.json"))
    try:
        with ExitStack() as stack:
            for module in (dialog_module, models_module, downloads_module):
                stack.enter_context(patch.object(module, "settings_manager", store))
            stack.enter_context(patch.object(dialog_module.history_manager, "set_retention"))
            for module in (models_module, downloads_module):
                stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
            stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
            dialog = dialog_module.SettingsDialog(
                background_cache_scan=False, backup_coordinator=coordinator,
            )
            try:
                assert BACKUP not in dialog._built_pages
                assert any(entry.destination == BACKUP and entry.title == "Schedule"
                           for entry in dialog._search_index())
                dialog.show()
                dialog.resize(dialog.MINIMUM_SIZE)
                dialog.select_destination(BACKUP)
                app.processEvents()
                assert BACKUP in dialog._built_pages
                assert dialog.backup_page._coordinator is coordinator
                assert dialog.page_title.text() == "Backup & restore"
                for control in (dialog.backup_page.create_button,
                                dialog.backup_page.inspect_button,
                                dialog.backup_page.save_schedule_button):
                    assert control.width() > 0
                    assert control.isVisibleTo(dialog)
                    right = control.mapTo(dialog.backup_page, QPoint(control.width(), 0)).x()
                    assert right <= dialog.backup_page.width()

                dialog.backup_page.set_busy(True)
                assert not dialog.rail.isEnabled()
                assert not dialog.search_button.isEnabled()
                dialog.select_destination("general")
                assert dialog.rail.current_key() == BACKUP
                dialog.open_search()
                assert not dialog.search_palette.isVisible()
                dialog.backup_page.set_busy(False)
                assert dialog.rail.isEnabled()
            finally:
                dialog.close()
    finally:
        app.setStyleSheet(previous_style)
