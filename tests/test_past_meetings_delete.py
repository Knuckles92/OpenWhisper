"""Qt tests for Past Meetings delete and clear-history actions."""

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QCheckBox, QMessageBox

from services.settings import SettingsKey
from ui_qt.widgets.past_meetings_panel import PastMeetingsPanel


def _meeting(meeting_id, *, has_audio=True, status="ended"):
    return {
        "id": meeting_id,
        "title": "Review",
        "status": status,
        "started_at": "2025-01-02T09:30:00",
        "ended_at": "2025-01-02T10:12:00",
        "content_summary": {
            "has_audio": has_audio,
            "has_transcript": True,
        },
    }


class TestPastMeetingsDelete:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setup_method(self):
        self.panel = PastMeetingsPanel(
            meeting_provider=lambda: [_meeting("m_done")]
        )
        self.panel.refresh()

    def teardown_method(self):
        self.panel.deleteLater()

    def test_delete_is_canceled_when_confirmation_is_rejected(self):
        deleted = []
        self.panel.delete_meeting_requested.connect(
            lambda meeting_id, delete_recordings: deleted.append(
                (meeting_id, delete_recordings)
            )
        )

        with (
            patch(
                "ui_qt.widgets.past_meetings_panel.settings_manager.get",
                return_value=True,
            ),
            patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.No,
            ),
        ):
            self.panel._confirm_delete("m_done")

        assert deleted == []

    def test_delete_keeps_recordings_by_default(self):
        deleted = []
        self.panel.delete_meeting_requested.connect(
            lambda meeting_id, delete_recordings: deleted.append(
                (meeting_id, delete_recordings)
            )
        )

        with (
            patch(
                "ui_qt.widgets.past_meetings_panel.settings_manager.get",
                return_value=True,
            ),
            patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QCheckBox, "isChecked", return_value=False),
        ):
            self.panel._confirm_delete("m_done")

        assert deleted == [("m_done", False)]

    def test_audio_spool_can_be_deleted_with_meeting(self):
        deleted = []
        self.panel.delete_meeting_requested.connect(
            lambda meeting_id, delete_recordings: deleted.append(
                (meeting_id, delete_recordings)
            )
        )

        with (
            patch(
                "ui_qt.widgets.past_meetings_panel.settings_manager.get",
                return_value=True,
            ),
            patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch(
                "ui_qt.widgets.past_meetings_panel.QComboBox.currentData",
                return_value=True,
            ),
            patch.object(QCheckBox, "isChecked", return_value=False),
        ):
            self.panel._confirm_delete("m_done")

        assert deleted == [("m_done", True)]

    def test_dont_ask_again_preference_is_saved(self):
        with (
            patch(
                "ui_qt.widgets.past_meetings_panel.settings_manager.get",
                return_value=True,
            ),
            patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QCheckBox, "isChecked", return_value=True),
            patch(
                "ui_qt.widgets.past_meetings_panel.settings_manager.save_setting"
            ) as save_setting,
        ):
            self.panel._confirm_delete("m_done")

        save_setting.assert_called_once_with(
            SettingsKey.CONFIRM_MEETING_DELETE,
            False,
        )

    def test_saved_opt_out_skips_confirmation_and_keeps_recordings(self):
        deleted = []
        self.panel.delete_meeting_requested.connect(
            lambda meeting_id, delete_recordings: deleted.append(
                (meeting_id, delete_recordings)
            )
        )

        with (
            patch(
                "ui_qt.widgets.past_meetings_panel.settings_manager.get",
                return_value=False,
            ),
            patch.object(QMessageBox, "exec") as show_confirmation,
        ):
            self.panel._confirm_delete("m_done")

        show_confirmation.assert_not_called()
        assert deleted == [("m_done", False)]

    def test_clear_history_emits_keep_recordings(self):
        cleared = []
        self.panel.clear_meetings_requested.connect(cleared.append)

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.panel._on_clear_history()

        assert cleared == [False]

    def test_clear_history_and_recordings_emits_delete_spools(self):
        cleared = []
        self.panel.clear_meetings_requested.connect(cleared.append)

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.panel._on_clear_history_and_recordings()

        assert cleared == [True]

    def test_clear_actions_are_disabled_when_empty(self):
        empty = PastMeetingsPanel(meeting_provider=lambda: [])
        empty.refresh()
        try:
            menu = empty._build_header_menu()
            actions = {
                action.text(): action
                for action in menu.actions()
                if not action.isSeparator()
            }
            assert actions["Clear history"].isEnabled() is False
            assert actions["Clear history + recordings"].isEnabled() is False
            assert actions["Export past meetings…"].isEnabled() is False
            assert actions["Refresh"].isEnabled() is True
            assert actions["Open meetings folder"].isEnabled() is True
        finally:
            empty.deleteLater()

    def test_clear_actions_are_enabled_when_meetings_exist(self):
        menu = self.panel._build_header_menu()
        actions = {
            action.text(): action
            for action in menu.actions()
            if not action.isSeparator()
        }
        assert actions["Clear history"].isEnabled() is True
        assert actions["Clear history + recordings"].isEnabled() is True
        assert actions["Export past meetings…"].isEnabled() is True
