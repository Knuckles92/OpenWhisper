"""Tests for the history card's "Show in Folder" action and its reveal helper."""

import os
import sys
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services.models import TranscriptionHistory
from ui_qt.utils import file_reveal
from ui_qt.widgets.history_sidebar import HistoryItemWidget


def _entry(audio_file=None):
    return TranscriptionHistory.create(
        text="hello there",
        model="base.en",
        audio_file=audio_file,
    )


def _menu_actions(widget):
    """Build the card's context menu without blocking on ``exec``."""
    captured = {}

    def _capture(self, *args, **kwargs):
        captured["actions"] = list(self.actions())
        return None

    with patch("ui_qt.widgets.history_sidebar.QMenu.exec", _capture):
        widget._show_context_menu(widget.rect().center())
    return captured["actions"]


def _find(actions, text):
    return next((a for a in actions if a.text() == text), None)


class TestShowInFolderAction:
    """The action is always listed, but only live with a recording on disk."""

    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_action_is_enabled_when_the_recording_exists(self, tmp_path):
        audio = tmp_path / "rec.wav"
        audio.write_bytes(b"RIFF")

        with patch(
            "ui_qt.widgets.history_sidebar.history_manager.get_recording_path",
            return_value=str(audio),
        ):
            widget = HistoryItemWidget(_entry("rec.wav"))

        action = _find(_menu_actions(widget), "Show in Folder")
        assert action is not None
        assert action.isEnabled()
        widget.deleteLater()

    def test_action_is_listed_but_disabled_without_a_recording(self):
        widget = HistoryItemWidget(_entry(None))

        action = _find(_menu_actions(widget), "Show in Folder")
        assert action is not None
        assert not action.isEnabled()
        widget.deleteLater()

    def test_triggering_reveals_the_recording_path(self, tmp_path):
        audio = tmp_path / "rec.wav"
        audio.write_bytes(b"RIFF")

        with patch(
            "ui_qt.widgets.history_sidebar.history_manager.get_recording_path",
            return_value=str(audio),
        ):
            widget = HistoryItemWidget(_entry("rec.wav"))

        with patch(
            "ui_qt.widgets.history_sidebar.reveal_in_file_manager",
            return_value=True,
        ) as reveal:
            _find(_menu_actions(widget), "Show in Folder").trigger()

        reveal.assert_called_once_with(str(audio))
        widget.deleteLater()

    def test_nothing_is_revealed_when_the_card_has_no_recording(self):
        widget = HistoryItemWidget(_entry(None))

        with patch(
            "ui_qt.widgets.history_sidebar.reveal_in_file_manager"
        ) as reveal:
            widget._on_show_in_folder()

        reveal.assert_not_called()
        widget.deleteLater()


class TestRevealInFileManager:
    """Platform dispatch and the open-the-folder fallback."""

    def test_empty_path_is_refused(self):
        assert file_reveal.reveal_in_file_manager("") is False

    def test_windows_selects_the_file_with_a_quoted_command_line(self, tmp_path):
        audio = tmp_path / "rec.wav"
        audio.write_bytes(b"RIFF")

        with (
            patch.object(file_reveal.sys, "platform", "win32"),
            patch.object(file_reveal.subprocess, "Popen") as popen,
        ):
            assert file_reveal.reveal_in_file_manager(str(audio)) is True

        # Explorer only honors the selection when the path is quoted after the
        # comma, so the command line must stay a single raw string.
        command = popen.call_args[0][0]
        assert command == f'explorer /select,"{os.path.abspath(audio)}"'

    def test_macos_reveals_with_open_dash_r(self, tmp_path):
        audio = tmp_path / "rec.wav"
        audio.write_bytes(b"RIFF")

        with (
            patch.object(file_reveal.sys, "platform", "darwin"),
            patch.object(file_reveal.subprocess, "run") as run,
        ):
            assert file_reveal.reveal_in_file_manager(str(audio)) is True

        assert run.call_args[0][0] == ["open", "-R", os.path.abspath(audio)]

    def test_other_platforms_open_the_containing_folder(self, tmp_path):
        audio = tmp_path / "rec.wav"
        audio.write_bytes(b"RIFF")

        with (
            patch.object(file_reveal.sys, "platform", "linux"),
            patch.object(
                file_reveal.QDesktopServices, "openUrl", return_value=True
            ) as open_url,
        ):
            assert file_reveal.reveal_in_file_manager(str(audio)) is True

        revealed = open_url.call_args[0][0].toLocalFile()
        assert os.path.normpath(revealed) == os.path.normpath(str(tmp_path))

    def test_a_deleted_recording_falls_back_to_its_folder(self, tmp_path):
        missing = tmp_path / "gone.wav"

        with (
            patch.object(file_reveal.subprocess, "Popen") as popen,
            patch.object(file_reveal.subprocess, "run") as run,
            patch.object(
                file_reveal.QDesktopServices, "openUrl", return_value=True
            ) as open_url,
        ):
            assert file_reveal.reveal_in_file_manager(str(missing)) is True

        popen.assert_not_called()
        run.assert_not_called()
        open_url.assert_called_once()

    def test_a_failed_selection_still_falls_back_to_the_folder(self, tmp_path):
        audio = tmp_path / "rec.wav"
        audio.write_bytes(b"RIFF")

        with (
            patch.object(file_reveal.sys, "platform", "win32"),
            patch.object(
                file_reveal.subprocess, "Popen", side_effect=OSError("boom")
            ),
            patch.object(
                file_reveal.QDesktopServices, "openUrl", return_value=True
            ) as open_url,
        ):
            assert file_reveal.reveal_in_file_manager(str(audio)) is True

        open_url.assert_called_once()

    def test_a_missing_folder_reports_failure(self, tmp_path):
        orphan = os.path.join(str(tmp_path), "vanished", "rec.wav")

        with patch.object(
            file_reveal.QDesktopServices, "openUrl"
        ) as open_url:
            assert file_reveal.reveal_in_file_manager(orphan) is False

        open_url.assert_not_called()
