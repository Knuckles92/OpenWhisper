"""Qt tests for the transcription-history export dialog."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from services.history_export import FORMAT_JSON, FORMAT_MARKDOWN
from ui_qt.dialogs.history_export_dialog import HistoryExportDialog


def _entry(
    entry_id,
    *,
    preview="Hello from history",
    timestamp="2026-03-15T14:30:00+00:00",
    formatted="Mar 15, 2026 02:30 PM",
    has_audio=False,
):
    return {
        "id": entry_id,
        "timestamp": timestamp,
        "formatted_timestamp": formatted,
        "preview_text": preview,
        "text": preview,
        "has_audio": has_audio,
        "audio_file": "recording.wav" if has_audio else None,
        "model": "local_whisper",
        "raw_text": None,
    }


class TestHistoryExportDialog:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, entries, **kwargs):
        return HistoryExportDialog(
            entry_provider=lambda: list(entries),
            **kwargs,
        )

    def test_empty_state_has_no_export_button(self):
        from ui_qt.widgets.wrapped_label import WrappedLabel

        dialog = self._dialog([])
        try:
            assert not hasattr(dialog, "export_btn")
            labels = [
                widget.text() for widget in dialog.findChildren(WrappedLabel)
            ]
            assert any(
                "No transcriptions to export yet." in text for text in labels
            )
        finally:
            dialog.deleteLater()

    def test_resolve_targets_filters_date_range(self):
        dialog = self._dialog([
            _entry("h_old", timestamp="2026-01-02T09:00:00+00:00", preview="Old"),
            _entry("h_mid", timestamp="2026-03-15T14:30:00+00:00", preview="Mid"),
            _entry("h_new", timestamp="2026-06-01T09:00:00+00:00", preview="New"),
        ])
        try:
            dialog.date_range_check.setChecked(True)
            dialog.from_date.setDate(QDate(2026, 3, 1))
            dialog.to_date.setDate(QDate(2026, 3, 31))
            targets = dialog._resolve_targets()
            assert [row["id"] for row in targets] == ["h_mid"]
        finally:
            dialog.deleteLater()

    def test_selected_scope_uses_checked_entries(self):
        dialog = self._dialog([
            _entry("h_a", preview="Alpha"),
            _entry("h_b", preview="Beta"),
        ])
        try:
            dialog.selected_radio.setChecked(True)
            dialog.selected_list.item(1).setCheckState(Qt.CheckState.Checked)
            dialog._refresh_export_enabled()
            targets = dialog._resolve_targets()
            assert [row["id"] for row in targets] == ["h_b"]
            assert dialog.export_btn.isEnabled()
        finally:
            dialog.deleteLater()

    def test_markdown_toggles_disable_for_json(self):
        dialog = self._dialog([_entry("h_a")])
        try:
            assert dialog.include_cleaned_check.isEnabled()
            assert dialog.include_raw_check.isEnabled()
            index = dialog.format_combo.findData(FORMAT_JSON)
            dialog.format_combo.setCurrentIndex(index)
            assert dialog._current_format() == FORMAT_JSON
            assert not dialog.include_cleaned_check.isEnabled()
            assert not dialog.include_raw_check.isEnabled()
            index = dialog.format_combo.findData(FORMAT_MARKDOWN)
            dialog.format_combo.setCurrentIndex(index)
            assert dialog.include_cleaned_check.isEnabled()
        finally:
            dialog.deleteLater()

    def test_default_path_is_absolute_and_follows_format(self, tmp_path, monkeypatch):
        recordings_root = tmp_path / "recordings"
        recordings_root.mkdir()
        monkeypatch.setattr(
            "ui_qt.dialogs.history_export_dialog.config.RECORDINGS_FOLDER",
            str(recordings_root),
            raising=False,
        )
        dialog = self._dialog([_entry("h_a")])
        try:
            path = dialog._default_path()
            assert os.path.isabs(path)
            assert path.endswith("openwhisper_history.md")
            assert os.path.dirname(path) == str(tmp_path)
            dialog.per_entry_check.setChecked(True)
            folder = dialog._default_path()
            assert folder.endswith("history_export")
        finally:
            dialog.deleteLater()

    def test_export_without_matches_does_not_start_worker(self):
        dialog = self._dialog([
            _entry("h_old", timestamp="2026-01-02T09:00:00+00:00"),
        ])
        try:
            dialog.date_range_check.setChecked(True)
            dialog.from_date.setDate(QDate(2026, 6, 1))
            dialog.to_date.setDate(QDate(2026, 6, 30))
            with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                QMessageBox, "information"
            ) as info:
                dialog._on_export()
            info.assert_called_once()
            assert dialog._worker is None
        finally:
            dialog.deleteLater()
