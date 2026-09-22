"""Retention controls apply finished values only, and ask before deleting.

The count spinbox used to apply every keystroke: replacing 20 with 100
applied 1 on the way and pruned all but one saved recording (2026-09-22).
"""
import os
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from config import config
from services.history_manager import HistoryManager
from services.settings import RecordingRetentionMode, SettingsKey, SettingsManager
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import RECORDING

MB = 1024 * 1024
MODE = SettingsKey.RECORDING_RETENTION_MODE
COUNT = SettingsKey.MAX_SAVED_RECORDINGS
SIZE_MB = SettingsKey.MAX_SAVED_RECORDINGS_MB
BY_COUNT = RecordingRetentionMode.CUSTOM
YES = QMessageBox.StandardButton.Yes
CANCEL = QMessageBox.StandardButton.Cancel


@pytest.fixture
def open_settings(tmp_path):
    """Open Settings on the Recording page over a temp recordings folder."""
    stack = ExitStack()

    def build(values, recordings, size_bytes=1024):
        folder = tmp_path / "recordings"
        folder.mkdir()
        for index in range(recordings):
            stamp = f"20260901_{index // 60:02d}{index % 60:02d}00"
            (folder / f"recording_{stamp}.wav").write_bytes(b"\0" * size_bytes)
        store = SettingsManager(str(tmp_path / "settings.json"))
        store.save_all_settings(values)
        manager = HistoryManager(recordings_folder=str(folder), max_recordings=None)

        stack.enter_context(patch("services.history_manager.db", MagicMock()))
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(
            patch.object(settings_dialog_module, "history_manager", manager)
        )
        for module in (models_module, downloads_module):
            stack.enter_context(
                patch.object(module, "scan_cached_models", return_value={})
            )
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        apply = stack.enter_context(
            patch.object(manager, "set_retention", wraps=manager.set_retention)
        )
        question = stack.enter_context(
            patch.object(settings_dialog_module.QMessageBox, "question")
        )
        dialog = settings_dialog_module.SettingsDialog(
            get_loaded_model=lambda: None, background_cache_scan=False
        )
        dialog.select_destination(RECORDING)
        dialog.show()
        QApplication.processEvents()
        return SimpleNamespace(
            dialog=dialog, store=store, folder=folder,
            apply=apply, question=question,
        )

    yield build
    stack.close()


def _type(spinbox, text):
    spinbox.setFocus()
    QApplication.processEvents()
    spinbox.selectAll()
    QTest.keyClicks(spinbox, text)


def _remaining(folder):
    return len(os.listdir(folder))


@pytest.mark.parametrize("commit", ["enter", "focus_out"])
def test_typing_100_over_20_never_applies_1_or_10(open_settings, commit):
    env = open_settings({MODE: BY_COUNT, COUNT: 20}, recordings=20)
    spinbox = env.dialog.max_recordings_spinbox

    _type(spinbox, "100")
    env.apply.assert_not_called()
    assert env.store.get(COUNT) == 20

    if commit == "enter":
        QTest.keyClick(spinbox, Qt.Key.Key_Return)
    else:
        env.dialog.recording_retention_combo.setFocus()
    QApplication.processEvents()

    assert env.apply.call_args_list == [call(100, None)]
    assert env.store.get(COUNT) == 100
    assert _remaining(env.folder) == 20
    env.question.assert_not_called()


def test_confirmed_shrink_prunes_only_the_oldest(open_settings):
    env = open_settings({MODE: BY_COUNT, COUNT: 20}, recordings=12)
    env.question.return_value = YES

    _type(env.dialog.max_recordings_spinbox, "5")
    QTest.keyClick(env.dialog.max_recordings_spinbox, Qt.Key.Key_Return)

    env.question.assert_called_once()
    title, body = env.question.call_args.args[1:3]
    assert title == "Delete 7 saved recordings?"
    assert "7 older saved recordings" in body
    assert env.apply.call_args_list == [call(5, None)]
    assert env.store.get(COUNT) == 5
    assert sorted(os.listdir(env.folder)) == [
        f"recording_20260901_00{index:02d}00.wav" for index in range(7, 12)
    ]


def test_declined_shrink_keeps_files_and_restores_the_count(open_settings):
    env = open_settings({MODE: BY_COUNT, COUNT: 20}, recordings=12)
    env.question.return_value = CANCEL

    _type(env.dialog.max_recordings_spinbox, "5")
    QTest.keyClick(env.dialog.max_recordings_spinbox, Qt.Key.Key_Return)
    # Focus leaving afterwards must not ask again or apply the typed value.
    env.dialog.recording_retention_combo.setFocus()
    QApplication.processEvents()

    env.question.assert_called_once()
    env.apply.assert_not_called()
    assert env.dialog.max_recordings_spinbox.value() == 20
    assert env.store.get(COUNT) == 20
    assert _remaining(env.folder) == 12


def test_chevron_burst_settles_into_one_prompt(open_settings):
    env = open_settings({MODE: BY_COUNT, COUNT: 20}, recordings=20)
    env.question.return_value = YES
    env.dialog._retention_commit_timer.setInterval(0)
    down = env.dialog.max_recordings_spinbox.down_button

    for _ in range(3):
        down.click()
    env.question.assert_not_called()
    env.apply.assert_not_called()
    assert env.dialog._retention_commit_timer.isActive()

    QTest.qWait(20)

    env.question.assert_called_once()
    assert env.question.call_args.args[1] == "Delete 3 saved recordings?"
    assert env.apply.call_args_list == [call(17, None)]
    assert _remaining(env.folder) == 17


def test_switching_keep_all_to_count_asks_first(open_settings):
    env = open_settings(
        {MODE: RecordingRetentionMode.KEEP_ALL, COUNT: 20}, recordings=25
    )
    combo = env.dialog.recording_retention_combo

    env.question.return_value = CANCEL
    combo.setCurrentIndex(combo.findData(BY_COUNT))

    assert combo.currentData() == RecordingRetentionMode.KEEP_ALL
    assert not env.dialog.max_recordings_spinbox.isEnabled()
    assert env.store.get(MODE) == RecordingRetentionMode.KEEP_ALL
    env.apply.assert_not_called()
    assert _remaining(env.folder) == 25

    env.question.return_value = YES
    combo.setCurrentIndex(combo.findData(BY_COUNT))

    assert env.question.call_count == 2
    assert env.store.get(MODE) == BY_COUNT
    assert env.apply.call_args_list == [call(20, None)]
    assert _remaining(env.folder) == 20


def test_typing_a_folder_size_never_applies_its_prefix(open_settings):
    env = open_settings(
        {MODE: RecordingRetentionMode.SIZE_LIMIT, SIZE_MB: 1000},
        recordings=10,
        size_bytes=2 * MB,
    )

    # 100 MB keeps all ten 2 MB files; the "10" on the way would keep five.
    _type(env.dialog.max_recordings_mb_spinbox, "100")
    env.apply.assert_not_called()
    QTest.keyClick(env.dialog.max_recordings_mb_spinbox, Qt.Key.Key_Return)

    assert env.apply.call_args_list == [call(None, 100 * MB)]
    assert _remaining(env.folder) == 10
    env.question.assert_not_called()


def test_live_applied_spinboxes_commit_typed_values_once(open_settings):
    env = open_settings({}, recordings=0)
    for spinbox in (
        env.dialog.max_recordings_spinbox,
        env.dialog.max_recordings_mb_spinbox,
        env.dialog.streaming_font_size_spinbox,
        env.dialog.meeting_port_spinbox,
    ):
        assert not spinbox.keyboardTracking()


def test_suite_keeps_recordings_out_of_the_checkout():
    repo_recordings = os.path.abspath("recordings")
    assert os.path.abspath(config.RECORDINGS_FOLDER) != repo_recordings
    folder = settings_dialog_module.history_manager.recordings_folder
    assert os.path.abspath(folder) != repo_recordings
