"""Qt tests for the Hugging Face consent dialog and Settings navigation."""
import os
import tempfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

from services.settings import HuggingFaceAccessPolicy, SettingsManager
from ui_qt.dialogs.hf_consent_dialog import HuggingFaceConsentDialog


class _QtTestCase:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])


class TestConsentDialogCopy(_QtTestCase):
    """Dialog copy: model identity, Hugging Face, size, local storage."""

    def test_ask_dialog_identifies_source_model_and_size(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)
        body = dialog._body_text()
        assert "base" in body
        assert "Hugging Face" in body
        assert "Systran/faster-whisper-base" in body
        # Bundled estimate shown without contacting Hugging Face
        assert "~145 MB" in body

    def test_unknown_model_omits_size_estimate(self):
        dialog = HuggingFaceConsentDialog(
            "someone/custom-model", HuggingFaceAccessPolicy.ASK
        )
        assert "Approximate download size" not in dialog._body_text()

    def test_never_dialog_explains_policy(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.NEVER)
        assert "Never connect" in dialog._body_text()

    def test_env_blocked_dialog_explains_environment(self):
        dialog = HuggingFaceConsentDialog(
            "base", HuggingFaceAccessPolicy.NEVER, env_blocked=True
        )
        assert "HF_HUB_OFFLINE" in dialog._body_text()


class TestConsentDialogButtons(_QtTestCase):
    """Button availability per policy, and the result each click produces."""

    def _button(self, dialog, name):
        return dialog.findChild(QPushButton, name)

    def test_ask_policy_buttons(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)
        assert self._button(dialog, "consentDownloadOnceButton") is not None
        assert self._button(dialog, "consentAlwaysAllowButton") is not None
        assert self._button(dialog, "consentCancelButton") is not None
        assert self._button(dialog, "consentOpenSettingsButton") is None

    def test_never_policy_buttons(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.NEVER)
        assert self._button(dialog, "consentDownloadOnceButton") is not None
        assert self._button(dialog, "consentOpenSettingsButton") is not None
        assert self._button(dialog, "consentCancelButton") is not None
        assert self._button(dialog, "consentAlwaysAllowButton") is None

    def test_env_blocked_offers_no_download_actions(self):
        dialog = HuggingFaceConsentDialog(
            "base", HuggingFaceAccessPolicy.ASK, env_blocked=True
        )
        assert self._button(dialog, "consentDownloadOnceButton") is None
        assert self._button(dialog, "consentAlwaysAllowButton") is None
        assert self._button(dialog, "consentCloseButton") is not None

    def test_download_once_result(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)
        self._button(dialog, "consentDownloadOnceButton").click()
        assert dialog.result_action == HuggingFaceConsentDialog.RESULT_DOWNLOAD_ONCE

    def test_always_allow_result(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)
        self._button(dialog, "consentAlwaysAllowButton").click()
        assert dialog.result_action == HuggingFaceConsentDialog.RESULT_ALWAYS_ALLOW

    def test_open_settings_result(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.NEVER)
        self._button(dialog, "consentOpenSettingsButton").click()
        assert dialog.result_action == HuggingFaceConsentDialog.RESULT_OPEN_SETTINGS

    def test_cancel_result(self):
        dialog = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)
        self._button(dialog, "consentCancelButton").click()
        assert dialog.result_action == HuggingFaceConsentDialog.RESULT_CANCEL


class TestSettingsDialogNavigation(_QtTestCase):
    """Open Settings must land directly on the Advanced/Hugging Face control."""

    def test_focus_hf_policy_selects_advanced_destination(self):
        from ui_qt.dialogs import settings_dialog as settings_dialog_module

        with tempfile.TemporaryDirectory() as tmp:
            isolated = SettingsManager(os.path.join(tmp, "settings.json"))
            with patch.object(
                settings_dialog_module, "settings_manager", isolated
            ):
                dialog = settings_dialog_module.SettingsDialog()
                dialog.focus_hf_policy()

                assert dialog.rail.current_key() == settings_dialog_module.ADVANCED
                policies = {
                    dialog.hf_policy_combo.itemData(i)
                    for i in range(dialog.hf_policy_combo.count())
                }
                assert policies == set(HuggingFaceAccessPolicy.ALL)


