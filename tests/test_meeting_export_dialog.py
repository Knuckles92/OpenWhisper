"""Qt tests for the Past Meetings export dialog."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from meeting.export.bulk import FORMAT_JSON, FORMAT_MARKDOWN
from ui_qt.dialogs.meeting_export_dialog import MeetingExportDialog


def _meeting(
    meeting_id,
    *,
    title="Review",
    started="2026-03-15T14:30:00",
    has_transcript=True,
):
    return {
        "id": meeting_id,
        "title": title,
        "status": "ended",
        "started_at": started,
        "content_summary": {"has_transcript": has_transcript},
    }


class TestMeetingExportDialog:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, meetings, **kwargs):
        return MeetingExportDialog(
            meeting_provider=lambda: list(meetings),
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
            assert any("No past meetings to export yet." in text for text in labels)
        finally:
            dialog.deleteLater()

    def test_resolve_targets_filters_date_range(self):
        dialog = self._dialog([
            _meeting("m_old", started="2026-01-02T09:00:00", title="Old"),
            _meeting("m_mid", started="2026-03-15T14:30:00", title="Mid"),
            _meeting("m_new", started="2026-06-01T09:00:00", title="New"),
        ])
        try:
            dialog.date_range_check.setChecked(True)
            dialog.from_date.setDate(QDate(2026, 3, 1))
            dialog.to_date.setDate(QDate(2026, 3, 31))
            targets = dialog._resolve_targets()
            assert [row["id"] for row in targets] == ["m_mid"]
        finally:
            dialog.deleteLater()

    def test_selected_scope_uses_checked_meetings(self):
        dialog = self._dialog([
            _meeting("m_a", title="Alpha"),
            _meeting("m_b", title="Beta"),
        ])
        try:
            dialog.selected_radio.setChecked(True)
            dialog.selected_list.item(1).setCheckState(Qt.CheckState.Checked)
            dialog._refresh_export_enabled()
            targets = dialog._resolve_targets()
            assert [row["id"] for row in targets] == ["m_b"]
            assert dialog.export_btn.isEnabled()
        finally:
            dialog.deleteLater()

    def test_markdown_toggles_disable_for_json(self):
        dialog = self._dialog([_meeting("m_a")])
        try:
            assert dialog.include_transcript_check.isEnabled()
            assert dialog.include_intelligence_check.isEnabled()
            index = dialog.format_combo.findData(FORMAT_JSON)
            dialog.format_combo.setCurrentIndex(index)
            assert dialog._current_format() == FORMAT_JSON
            assert not dialog.include_transcript_check.isEnabled()
            assert not dialog.include_intelligence_check.isEnabled()
            index = dialog.format_combo.findData(FORMAT_MARKDOWN)
            dialog.format_combo.setCurrentIndex(index)
            assert dialog.include_transcript_check.isEnabled()
        finally:
            dialog.deleteLater()

    def test_default_path_is_absolute_and_follows_format(self, tmp_path, monkeypatch):
        meetings_root = tmp_path / "meetings"
        meetings_root.mkdir()
        monkeypatch.setattr(
            "ui_qt.dialogs.meeting_export_dialog.config.MEETINGS_FOLDER",
            str(meetings_root),
            raising=False,
        )
        dialog = self._dialog([_meeting("m_a")])
        try:
            path = dialog._default_path()
            assert os.path.isabs(path)
            assert path.endswith("openwhisper_meetings.md")
            assert os.path.dirname(path) == str(tmp_path)
            dialog.per_meeting_check.setChecked(True)
            folder = dialog._default_path()
            assert folder.endswith("meetings_export")
        finally:
            dialog.deleteLater()

    def test_export_without_matches_does_not_start_worker(self):
        dialog = self._dialog([
            _meeting("m_old", started="2026-01-02T09:00:00"),
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
