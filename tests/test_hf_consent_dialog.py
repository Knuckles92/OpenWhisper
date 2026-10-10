"""Qt tests for the Hugging Face consent dialog and Settings navigation."""
import os
import tempfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

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
        # Bundled estimate shown without contacting Hugging Face, once: in
        # the title, which never scrolls out of view.
        assert dialog.title_label.text() == 'Download "base" (about 145 MB)?'
        assert "145 MB" not in body

    def test_large_model_title_carries_its_size(self):
        dialog = HuggingFaceConsentDialog("large-v3", HuggingFaceAccessPolicy.ASK)
        assert dialog.title_label.text() == 'Download "large-v3" (about 3.1 GB)?'

    def test_unknown_model_omits_size_estimate(self):
        dialog = HuggingFaceConsentDialog(
            "someone/custom-model", HuggingFaceAccessPolicy.ASK
        )
        assert "Approximate download size" not in dialog._body_text()
        assert dialog.title_label.text() == 'Download "someone/custom-model" model?'

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

    def test_cancel_is_the_default_and_focused_button(self):
        for policy in (HuggingFaceAccessPolicy.ASK, HuggingFaceAccessPolicy.NEVER):
            dialog = HuggingFaceConsentDialog("large-v3", policy)
            defaults = [b.objectName() for b in dialog.findChildren(QPushButton) if b.isDefault()]
            assert defaults == ["consentCancelButton"]
            assert dialog.focusWidget() is self._button(dialog, "consentCancelButton")
            assert not self._button(dialog, "consentDownloadOnceButton").autoDefault()
        always = self._button(
            HuggingFaceConsentDialog("large-v3", HuggingFaceAccessPolicy.ASK),
            "consentAlwaysAllowButton",
        )
        assert not always.autoDefault()

    def test_enter_does_not_start_the_download(self):
        dialog = HuggingFaceConsentDialog("large-v3", HuggingFaceAccessPolicy.ASK)
        dialog.show()
        self.app.processEvents()
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        assert dialog.result_action == HuggingFaceConsentDialog.RESULT_CANCEL
        assert dialog.result() == dialog.DialogCode.Rejected

        # Not even with the download button focused: it is not auto-default.
        dialog = HuggingFaceConsentDialog("large-v3", HuggingFaceAccessPolicy.ASK)
        dialog.show()
        self.app.processEvents()
        self._button(dialog, "consentDownloadOnceButton").setFocus()
        QTest.keyClick(dialog, Qt.Key.Key_Enter)
        assert dialog.result_action == HuggingFaceConsentDialog.RESULT_CANCEL
        dialog.close()


class TestConsentDialogShortScreen(_QtTestCase):
    """At 1024x640 the title, size and buttons stay in view; the notice scrolls."""

    def _shown(self, available_height):
        dialog = HuggingFaceConsentDialog("large-v3", HuggingFaceAccessPolicy.ASK)
        dialog._available_height = lambda: available_height
        dialog.show()
        self.app.processEvents()
        return dialog

    def test_a_short_screen_scrolls_the_notice_and_keeps_the_buttons(self):
        dialog = self._shown(400)
        try:
            assert dialog.height() <= 400 - dialog.SCREEN_MARGIN
            bar = dialog.body_scroll.verticalScrollBar()
            assert bar.maximum() > 0 and bar.value() == 0
            for name in ("consentCancelButton", "consentDownloadOnceButton"):
                button = dialog.findChild(QPushButton, name)
                bottom = button.mapTo(dialog, button.rect().bottomLeft()).y()
                assert bottom <= dialog.height()
            assert dialog.title_label.isVisible()
        finally:
            dialog.close()

    def test_a_tall_screen_shows_the_whole_notice(self):
        dialog = self._shown(2000)
        try:
            assert dialog.body_scroll.verticalScrollBar().maximum() == 0
        finally:
            dialog.close()


class TestSettingsDialogNavigation(_QtTestCase):
    """Open Settings must land directly on the Hugging Face policy control."""

    def test_focus_hf_policy_selects_downloads_destination(self):
        from ui_qt.dialogs import settings_dialog as settings_dialog_module

        with tempfile.TemporaryDirectory() as tmp:
            isolated = SettingsManager(os.path.join(tmp, "settings.json"))
            with patch.object(
                settings_dialog_module, "settings_manager", isolated
            ):
                dialog = settings_dialog_module.SettingsDialog(
                    background_cache_scan=False
                )
                dialog.focus_hf_policy()

                assert dialog.rail.current_key() == settings_dialog_module.DOWNLOADS
                assert dialog.downloads.isAncestorOf(dialog.hf_policy_combo)
                policies = {
                    dialog.hf_policy_combo.itemData(i)
                    for i in range(dialog.hf_policy_combo.count())
                }
                assert policies == set(HuggingFaceAccessPolicy.ALL)




def test_optional_model_consent_explains_required_runtime():
    _app = QApplication.instance() or QApplication([])
    with patch('services.local_asr.catalog.missing_runtime', return_value='asr-nvidia-cpu'), \
            patch('services.local_asr.catalog.gpu_runtime_offer', return_value=None):
        dialog = HuggingFaceConsentDialog('parakeet-v3', HuggingFaceAccessPolicy.ASK)
        body = dialog._body_text()
    assert 'Parakeet TDT 0.6B v3' in body
    assert 'NVIDIA Speech CPU is also required' in body
    assert 'download alone will not enable transcription' in body
    dialog.close()


def test_optional_model_consent_names_both_runtimes_when_the_gpu_is_offered():
    _app = QApplication.instance() or QApplication([])
    with patch('services.local_asr.catalog.missing_runtime', return_value='asr-nvidia-cpu'), \
            patch('services.local_asr.catalog.gpu_runtime_offer', return_value='asr-nvidia-cuda'):
        dialog = HuggingFaceConsentDialog('parakeet-v3', HuggingFaceAccessPolicy.ASK)
        body = dialog._body_text()
    assert "NVIDIA Speech GPU for this computer's NVIDIA GPU, or NVIDIA Speech CPU" in body
    assert 'asked which runtime to install' in body
    assert 'download alone will not enable transcription' in body
    dialog.close()


def _gpu_runtime_dialog(model_name, gpu_component, gpu):
    from ui_qt.dialogs.required_runtime_dialog import GpuRuntimeDialog
    _app = QApplication.instance() or QApplication([])
    with patch('services.gpu_info.nvidia_gpu', return_value=gpu):
        return GpuRuntimeDialog(model_name, gpu_component, 'asr-nvidia-cpu')


def test_gpu_runtime_offer_names_the_card_sizes_and_choices():
    from services.components import catalog_entry_for_platform
    from services.format_utils import format_size_bytes
    from services.gpu_info import NvidiaGpu

    dialog = _gpu_runtime_dialog('parakeet-v3', 'asr-nvidia-cuda', NvidiaGpu('NVIDIA GeForce RTX 2060', 6144, (7, 5)))
    title = dialog.findChild(QLabel, 'headerLabel').text()
    body = dialog.body.text().replace(' ', ' ')
    assert title == 'Run Parakeet on the RTX 2060?'
    assert "This computer's NVIDIA GeForce RTX 2060 can run it" in body
    assert 'NVIDIA Speech GPU runs the model on the RTX 2060 with CUDA' in body
    for component in ('asr-nvidia-cuda', 'asr-nvidia-cpu'):
        entry = catalog_entry_for_platform(component) or {}
        download = sum(archive['size_bytes'] for archive in entry.get('archives', []))
        assert (f"{format_size_bytes(download)} download, "
                f"{format_size_bytes(entry.get('install_bytes', 0))} on disk") in body
    buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
    assert set(buttons) == {'Later', 'Use the CPU', 'Use this GPU'}
    # Enter on a prompt that just appeared must not start a download.
    assert [text for text, button in buttons.items() if button.isDefault()] == ['Later']
    assert not buttons['Use this GPU'].autoDefault() and not buttons['Use the CPU'].autoDefault()
    buttons['Use this GPU'].click()
    assert dialog.result() == dialog.DialogCode.Accepted and dialog.choice == 'asr-nvidia-cuda'


def test_gpu_runtime_offer_cpu_and_later_answers():
    from services.gpu_info import NvidiaGpu

    gtx = NvidiaGpu('NVIDIA GeForce GTX 1050 Ti', 4096, (6, 1))
    dialog = _gpu_runtime_dialog('nemotron-3.5', 'asr-nvidia-vulkan', gtx)
    assert dialog.findChild(QLabel, 'headerLabel').text() == 'Run Nemotron Streaming on the GTX 1050 Ti?'
    # The card's name and each size stay on one line.
    assert 'on the GTX 1050 Ti with Vulkan' in dialog.body.text()
    buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
    buttons['Use the CPU'].click()
    assert dialog.result() == dialog.DialogCode.Accepted and dialog.choice == 'asr-nvidia-cpu'
    dialog = _gpu_runtime_dialog('nemotron-3.5', 'asr-nvidia-vulkan', gtx)
    buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
    buttons['Later'].click()
    assert dialog.result() == dialog.DialogCode.Rejected and dialog.choice is None


def test_runtime_prompt_has_explicit_install_and_later_actions():
    from ui_qt.dialogs.required_runtime_dialog import RequiredRuntimeDialog
    _app = QApplication.instance() or QApplication([])
    dialog = RequiredRuntimeDialog('parakeet-v3', 'asr-nvidia-cpu')
    assert 'needs both its model files and NVIDIA Speech CPU to work' in dialog.body.text()
    buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
    assert [text for text, button in buttons.items() if button.isDefault()] == ['Later']
    assert not buttons['Install required runtime'].autoDefault()
    buttons['Install required runtime'].click()
    assert dialog.result() == dialog.DialogCode.Accepted
    dialog = RequiredRuntimeDialog('parakeet-v3', 'asr-nvidia-cpu')
    buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
    buttons['Later'].click()
    assert dialog.result() == dialog.DialogCode.Rejected
