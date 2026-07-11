"""Qt tests for the Hugging Face consent dialog and Settings navigation."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

from services.settings import HuggingFaceAccessPolicy, SettingsManager
from ui_qt.dialogs.hf_consent_dialog import HuggingFaceConsentDialog


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class TestConsentDialogCopy(_QtTestCase):
    """Dialog copy: model identity, Hugging Face, size, local storage."""

    def test_ask_dialog_identifies_source_model_and_size(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)
        body = dialog._body_text()
        self.assertIn("base", body)
        self.assertIn("Hugging Face", body)
        self.assertIn("Systran/faster-whisper-base", body)
        # Bundled estimate shown without contacting Hugging Face
        self.assertIn("~145 MB", body)

    def test_unknown_model_omits_size_estimate(self):
        dialog = HuggingFaceConsentDialog(
            "someone/custom-model", HuggingFaceAccessPolicy.ASK
        )
        self.assertNotIn("Approximate download size", dialog._body_text())

    def test_never_dialog_explains_policy(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.NEVER)
        self.assertIn("Never connect", dialog._body_text())

    def test_env_blocked_dialog_explains_environment(self):
        dialog = HuggingFaceConsentDialog(
            "base", HuggingFaceAccessPolicy.NEVER, env_blocked=True
        )
        self.assertIn("HF_HUB_OFFLINE", dialog._body_text())


class TestConsentDialogButtons(_QtTestCase):
    """Button availability per policy, and the result each click produces."""

    def _button(self, dialog, name):
        return dialog.findChild(QPushButton, name)

    def test_ask_policy_buttons(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)
        self.assertIsNotNone(self._button(dialog, "consentDownloadOnceButton"))
        self.assertIsNotNone(self._button(dialog, "consentAlwaysAllowButton"))
        self.assertIsNotNone(self._button(dialog, "consentCancelButton"))
        self.assertIsNone(self._button(dialog, "consentOpenSettingsButton"))

    def test_never_policy_buttons(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.NEVER)
        self.assertIsNotNone(self._button(dialog, "consentDownloadOnceButton"))
        self.assertIsNotNone(self._button(dialog, "consentOpenSettingsButton"))
        self.assertIsNotNone(self._button(dialog, "consentCancelButton"))
        self.assertIsNone(self._button(dialog, "consentAlwaysAllowButton"))

    def test_env_blocked_offers_no_download_actions(self):
        dialog = HuggingFaceConsentDialog(
            "base", HuggingFaceAccessPolicy.ASK, env_blocked=True
        )
        self.assertIsNone(self._button(dialog, "consentDownloadOnceButton"))
        self.assertIsNone(self._button(dialog, "consentAlwaysAllowButton"))
        self.assertIsNotNone(self._button(dialog, "consentCloseButton"))

    def test_download_once_result(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)
        self._button(dialog, "consentDownloadOnceButton").click()
        self.assertEqual(
            dialog.result_action, HuggingFaceConsentDialog.RESULT_DOWNLOAD_ONCE
        )

    def test_always_allow_result(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)
        self._button(dialog, "consentAlwaysAllowButton").click()
        self.assertEqual(
            dialog.result_action, HuggingFaceConsentDialog.RESULT_ALWAYS_ALLOW
        )

    def test_open_settings_result(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.NEVER)
        self._button(dialog, "consentOpenSettingsButton").click()
        self.assertEqual(
            dialog.result_action, HuggingFaceConsentDialog.RESULT_OPEN_SETTINGS
        )

    def test_cancel_result(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)
        self._button(dialog, "consentCancelButton").click()
        self.assertEqual(
            dialog.result_action, HuggingFaceConsentDialog.RESULT_CANCEL
        )


class TestSettingsDialogNavigation(_QtTestCase):
    """Open Settings must land directly on the Advanced/Hugging Face control."""

    def test_focus_hf_policy_selects_advanced_tab(self):
        from ui_qt.dialogs import settings_dialog as settings_dialog_module

        with tempfile.TemporaryDirectory() as tmp:
            isolated = SettingsManager(os.path.join(tmp, "settings.json"))
            with patch.object(
                settings_dialog_module, "settings_manager", isolated
            ):
                dialog = settings_dialog_module.SettingsDialog()
                dialog.focus_hf_policy()

                self.assertEqual(
                    dialog.tabs.currentIndex(), dialog._advanced_tab_index
                )
                # The three-policy control replaces the old offline checkbox
                policies = {
                    dialog.hf_policy_combo.itemData(i)
                    for i in range(dialog.hf_policy_combo.count())
                }
                self.assertEqual(policies, set(HuggingFaceAccessPolicy.ALL))


if __name__ == "__main__":
    unittest.main()
