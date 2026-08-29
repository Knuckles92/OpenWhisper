"""Behavior tests for the Settings → API keys destination."""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox

from services import credentials
from services.settings import SettingsManager
from services.text_llm import upsert_custom_profile
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs.settings_dialog import API_KEYS, SettingsDialog

SECRET = "sk-proj-test-secret-value-7890wxyz"
KEY_NAMES = ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "LMSTUDIO_API_KEY")


def _unavailable_backend():
    raise RuntimeError("no keyring daemon")


class TestApiKeysSettingsPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.settings_path = os.path.join(self._temp.name, "settings.json")
        self.manager = SettingsManager(self.settings_path)
        self.manager.save_all_settings({})
        env_overrides = {name: "" for name in KEY_NAMES}
        self._patches = [
            patch.object(settings_dialog_module, "settings_manager", self.manager),
            patch.object(
                credentials, "env_file_path",
                lambda: os.path.join(self._temp.name, ".env"),
            ),
            patch.dict(os.environ, env_overrides),
        ]
        for patcher in self._patches:
            patcher.start()
        for name in KEY_NAMES:
            os.environ.pop(name, None)
        self.changes = []
        self.dialog = SettingsDialog()
        self.dialog.on_api_keys_changed = lambda: self.changes.append(True)

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        for patcher in reversed(self._patches):
            patcher.stop()
        self._temp.cleanup()

    def _select(self, env_name):
        combo = self.dialog.api_key_combo
        combo.setCurrentIndex(combo.findData(env_name))

    def _settings_file_text(self):
        with open(self.settings_path, encoding="utf-8") as handle:
            return handle.read()

    def test_lists_builtin_and_custom_credentials(self):
        settings = self.manager.load_all_settings()
        upsert_custom_profile(
            settings,
            name="LM Studio",
            base_url="http://127.0.0.1:1234/v1",
            api_key_env="LMSTUDIO_API_KEY",
        )
        upsert_custom_profile(
            settings, name="Ollama", base_url="http://127.0.0.1:11434/v1"
        )
        self.manager.save_all_settings(settings)
        self.dialog.refresh()

        combo = self.dialog.api_key_combo
        names = [combo.itemData(i) for i in range(combo.count())]
        self.assertEqual(names, list(KEY_NAMES))
        self.assertEqual(combo.itemText(2), "LM Studio · LMSTUDIO_API_KEY")
        self.assertEqual(self.dialog.rail.value(API_KEYS), "0 of 3 set")
        self.assertIn("No key set", self.dialog.api_key_status.text())

    def test_field_hides_input_until_show_is_toggled(self):
        edit = self.dialog.api_key_edit
        self.assertEqual(edit.echoMode(), QLineEdit.EchoMode.Password)
        self.dialog.api_key_show_button.setChecked(True)
        self.assertEqual(edit.echoMode(), QLineEdit.EchoMode.Normal)
        self.assertEqual(self.dialog.api_key_show_button.text(), "Hide")
        self.dialog.api_key_show_button.setChecked(False)
        self.assertEqual(edit.echoMode(), QLineEdit.EchoMode.Password)

    def test_save_stores_key_outside_settings_and_masks_it(self):
        self._select("OPENAI_API_KEY")
        self.assertFalse(self.dialog.api_key_save_button.isEnabled())
        self.dialog.api_key_edit.setText(f" {SECRET} ")
        self.assertTrue(self.dialog.api_key_save_button.isEnabled())
        self.dialog.api_key_show_button.setChecked(True)

        self.dialog._save_api_key()

        self.assertEqual(credentials.store().get("OPENAI_API_KEY"), SECRET)
        self.assertEqual(self.dialog.api_key_edit.text(), "")
        self.assertEqual(
            self.dialog.api_key_edit.echoMode(), QLineEdit.EchoMode.Password
        )
        status = self.dialog.api_key_status.text()
        self.assertIn("Saved in in-memory store", status)
        self.assertIn("••••wxyz", status)
        self.assertNotIn(SECRET, status)
        self.assertEqual(self.dialog.api_key_status.property("tone"), "success")
        self.assertEqual(self.dialog.message_label.text(), "OpenAI API key saved.")
        self.assertEqual(self.dialog.rail.value(API_KEYS), "1 of 2 set")
        self.assertTrue(self.dialog.api_key_remove_button.isEnabled())
        self.assertEqual(self.changes, [True])
        self.assertNotIn(SECRET, self._settings_file_text())
        self.assertNotIn("wxyz", self._settings_file_text())

    def test_invalid_key_is_rejected_before_storage(self):
        self._select("OPENAI_API_KEY")
        self.dialog.api_key_edit.setText("sk-has a space")
        self.dialog._save_api_key()
        self.assertIsNone(credentials.store().get("OPENAI_API_KEY"))
        self.assertIn("space", self.dialog.message_label.text())
        self.assertEqual(self.changes, [])

    def test_environment_value_is_reported_and_shadowed_by_saved_key(self):
        os.environ["OPENROUTER_API_KEY"] = "sk-or-environment-value-0001"
        self._select("OPENROUTER_API_KEY")
        self.dialog.refresh()
        self._select("OPENROUTER_API_KEY")
        status = self.dialog.api_key_status.text()
        self.assertIn("OPENROUTER_API_KEY environment variable", status)
        self.assertIn("••••0001", status)
        self.assertFalse(self.dialog.api_key_remove_button.isEnabled())
        self.assertTrue(self.dialog.api_key_test_button.isEnabled())

        self.dialog.api_key_edit.setText(SECRET)
        self.dialog._save_api_key()
        status = self.dialog.api_key_status.text()
        self.assertIn("••••wxyz", status)
        self.assertIn("ignored while a key is saved here", status)

        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ):
            self.dialog._remove_api_key()
        self.assertIsNone(credentials.store().get("OPENROUTER_API_KEY"))
        self.assertIn(
            "OPENROUTER_API_KEY environment variable",
            self.dialog.api_key_status.text(),
        )
        self.assertEqual(self.dialog.message_label.text(), "OpenRouter API key removed.")
        self.assertEqual(self.changes, [True, True])

    def test_declined_removal_keeps_key(self):
        credentials.store().set("OPENAI_API_KEY", SECRET)
        self._select("OPENAI_API_KEY")
        self.dialog.refresh()
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.No
        ):
            self.dialog._remove_api_key()
        self.assertEqual(credentials.store().get("OPENAI_API_KEY"), SECRET)
        self.assertEqual(self.changes, [])

    def test_unavailable_store_disables_saving_and_explains(self):
        previous = credentials.set_store(
            credentials.CredentialStore(backend_factory=_unavailable_backend)
        )
        try:
            self.dialog.refresh()
            self._select("OPENAI_API_KEY")
            self.dialog.api_key_edit.setText(SECRET)
            self.assertFalse(self.dialog.api_key_save_button.isEnabled())
            self.assertFalse(self.dialog.api_key_remove_button.isEnabled())
            caption = self.dialog.api_key_store_caption.text()
            self.assertIn("no keyring daemon", caption)
            self.assertIn("never falls back to an unprotected file", caption)
        finally:
            credentials.set_store(previous)

    def test_test_button_reports_verdict_without_echoing_key(self):
        self._select("OPENAI_API_KEY")
        self.dialog.api_key_edit.setText(SECRET)
        seen = {}

        def fake_verify(profile, key):
            seen["profile"] = profile.id
            seen["key"] = key
            return False, "api.openai.com rejected the key (HTTP 401)."

        with patch.object(settings_dialog_module, "verify_api_key", fake_verify):
            self.dialog._test_api_key()
            self.assertTrue(self.dialog._api_key_testing)
            self.assertFalse(self.dialog.api_key_save_button.isEnabled())
            deadline = time.monotonic() + 5
            while self.dialog._api_key_testing and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

        self.assertFalse(self.dialog._api_key_testing)
        self.assertEqual(seen, {"profile": "openai", "key": SECRET})
        message = self.dialog.message_label.text()
        self.assertEqual(message, "OpenAI API key failed: api.openai.com rejected the key (HTTP 401).")
        self.assertNotIn(SECRET, message)
        # A test never persists anything.
        self.assertIsNone(credentials.store().get("OPENAI_API_KEY"))
        self.assertEqual(self.dialog.api_key_edit.text(), SECRET)

    def test_focus_api_keys_selects_destination_and_credential(self):
        self.dialog.focus_api_keys("OPENROUTER_API_KEY")
        self.assertEqual(self.dialog.rail.current_key(), API_KEYS)
        self.assertEqual(self.dialog.api_key_combo.currentData(), "OPENROUTER_API_KEY")


if __name__ == "__main__":
    unittest.main()
