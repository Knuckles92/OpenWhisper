"""Tests for the past meeting card's "Show in Folder" action."""

import os
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui_qt.utils import file_reveal
from ui_qt.widgets.past_meetings_panel import PastMeetingItem


def _meeting(spool_dir=None):
    row = {
        "id": "m_abc1234567",
        "title": "Review",
        "status": "ended",
        "started_at": datetime(2025, 1, 2, 9, 30).isoformat(),
        "content_summary": {"has_transcript": True, "preview_text": "hello"},
    }
    if spool_dir is not None:
        row["spool_dir"] = spool_dir
    return row


def _menu_actions(widget):
    """Build the card's context menu without blocking on ``exec``."""
    captured = {}

    def _capture(self, *args, **kwargs):
        captured["actions"] = list(self.actions())
        return None

    with patch("ui_qt.widgets.past_meetings_panel.QMenu.exec", _capture):
        widget._show_context_menu(widget.rect().center())
    return captured["actions"]


def _find(actions, text):
    return next((a for a in actions if a.text() == text), None)


class TestShowInFolderAction:
    """Listed on every card, live only while the spool folder survives."""

    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_action_is_enabled_when_the_spool_folder_exists(self, tmp_path):
        spool = tmp_path / "m_abc1234567"
        spool.mkdir()
        (spool / "mic_session.wav").write_bytes(b"RIFF")

        widget = PastMeetingItem(_meeting(str(spool)))

        action = _find(_menu_actions(widget), "Show in Folder")
        assert action is not None
        assert action.isEnabled()
        widget.deleteLater()

    def test_action_is_listed_but_disabled_without_a_spool_dir(self):
        widget = PastMeetingItem(_meeting(None))

        action = _find(_menu_actions(widget), "Show in Folder")
        assert action is not None
        assert not action.isEnabled()
        widget.deleteLater()

    def test_action_is_disabled_once_the_recordings_are_cleared(self, tmp_path):
        # "Clear history + recordings" removes the folder but the row keeps
        # its spool_dir, so existence — not the string — gates the action.
        widget = PastMeetingItem(_meeting(str(tmp_path / "gone")))

        action = _find(_menu_actions(widget), "Show in Folder")
        assert action is not None
        assert not action.isEnabled()
        widget.deleteLater()

    def test_a_relative_spool_dir_resolves_against_the_working_directory(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "meetings" / "m_abc1234567").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        widget = PastMeetingItem(_meeting(os.path.join("meetings", "m_abc1234567")))

        assert _find(_menu_actions(widget), "Show in Folder").isEnabled()
        widget.deleteLater()

    def test_triggering_opens_the_meeting_folder(self, tmp_path):
        spool = tmp_path / "m_abc1234567"
        spool.mkdir()

        widget = PastMeetingItem(_meeting(str(spool)))

        with patch(
            "ui_qt.widgets.past_meetings_panel.open_folder_in_file_manager",
            return_value=True,
        ) as open_folder:
            _find(_menu_actions(widget), "Show in Folder").trigger()

        open_folder.assert_called_once_with(os.path.abspath(str(spool)))
        widget.deleteLater()

    def test_nothing_is_opened_when_the_card_has_no_folder(self):
        widget = PastMeetingItem(_meeting(None))

        with patch(
            "ui_qt.widgets.past_meetings_panel.open_folder_in_file_manager"
        ) as open_folder:
            widget._on_show_in_folder()

        open_folder.assert_not_called()
        widget.deleteLater()


class TestOpenFolderInFileManager:
    """The folder itself opens — not its parent with the folder selected."""

    def test_empty_path_is_refused(self):
        assert file_reveal.open_folder_in_file_manager("") is False

    def test_the_folder_itself_is_opened(self, tmp_path):
        spool = tmp_path / "m_abc1234567"
        spool.mkdir()

        with patch.object(
            file_reveal.QDesktopServices, "openUrl", return_value=True
        ) as open_url:
            assert file_reveal.open_folder_in_file_manager(str(spool)) is True

        opened = open_url.call_args[0][0].toLocalFile()
        assert os.path.normpath(opened) == os.path.normpath(str(spool))

    def test_a_missing_folder_reports_failure(self, tmp_path):
        with patch.object(file_reveal.QDesktopServices, "openUrl") as open_url:
            assert (
                file_reveal.open_folder_in_file_manager(str(tmp_path / "gone"))
                is False
            )

        open_url.assert_not_called()
