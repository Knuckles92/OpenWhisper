"""Qt tests for the Meeting tab knowledge-folder picker."""
import os
import tempfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs import settings_dialog as settings_dialog_module


class _DialogTestCase:
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class TestKnowledgeFolderSettings(_DialogTestCase):
    def _open(self, isolated):
        return patch.object(
            settings_dialog_module, "settings_manager", isolated,
        ), patch.object(
            settings_dialog_module.history_manager, "set_max_recordings",
        )

    def test_load_save_and_clear_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = os.path.join(temp_dir, "vault")
            os.mkdir(folder)
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            isolated.save_all_settings({
                SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED: True,
                SettingsKey.MEETING_CONTEXT_FOLDER_PATH: folder,
                SettingsKey.MEETING_WHISPER_MODEL: "tiny",
            })
            settings_patch, history_patch = self._open(isolated)
            with settings_patch, history_patch:
                dialog = settings_dialog_module.SettingsDialog()
                assert dialog.meeting_context_folder_check.isChecked()
                assert dialog.meeting_context_folder_path.text() == os.path.normpath(folder)
                dialog._clear_context_folder()
                assert not dialog.meeting_context_folder_check.isChecked()
                assert dialog.meeting_context_folder_path.text() == ""
                dialog._save_settings()

            saved = isolated.load_all_settings()
            assert not saved[SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED]
            assert saved[SettingsKey.MEETING_CONTEXT_FOLDER_PATH] == ""
            assert saved[SettingsKey.MEETING_WHISPER_MODEL] == "tiny"

    def test_browse_sets_path_and_enables_search(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = os.path.join(temp_dir, "notes")
            os.mkdir(folder)
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            settings_patch, history_patch = self._open(isolated)
            with settings_patch, history_patch, patch.object(
                settings_dialog_module.QFileDialog,
                "getExistingDirectory",
                return_value=folder,
            ):
                dialog = settings_dialog_module.SettingsDialog()
                assert not dialog.meeting_context_folder_check.isChecked()
                dialog._browse_context_folder()
                assert dialog.meeting_context_folder_check.isChecked()
                assert dialog.meeting_context_folder_path.text() == os.path.normpath(folder)
                dialog._save_settings()

            saved = isolated.load_all_settings()
            assert saved[SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED]
            assert saved[SettingsKey.MEETING_CONTEXT_FOLDER_PATH] == os.path.normpath(folder)

    def test_saving_preserves_unrelated_meeting_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            isolated.save_all_settings({
                SettingsKey.MEETING_WHISPER_MODEL: "tiny",
                SettingsKey.MEETING_LLM_PROVIDER: "openai",
                SettingsKey.MEETING_LLM_MODEL: "gpt-4o-mini",
            })
            settings_patch, history_patch = self._open(isolated)
            with settings_patch, history_patch:
                dialog = settings_dialog_module.SettingsDialog()
                dialog.meeting_context_folder_check.setChecked(True)
                dialog.meeting_context_folder_path.setText(
                    os.path.join(temp_dir, "missing-vault")
                )
                dialog._save_settings()

            saved = isolated.load_all_settings()
            assert saved[SettingsKey.MEETING_WHISPER_MODEL] == "tiny"
            assert saved[SettingsKey.MEETING_LLM_PROVIDER] == "openai"
            assert saved[SettingsKey.MEETING_LLM_MODEL] == "gpt-4o-mini"
            assert saved[SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED]
            assert os.path.isabs(saved[SettingsKey.MEETING_CONTEXT_FOLDER_PATH])
