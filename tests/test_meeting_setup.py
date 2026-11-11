"""First-run setup with fake downloads and platform capture devices."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QDialog

from meeting.interfaces import CHANNEL_LOOPBACK, CHANNEL_MIC, CaptureBlock
from services import meeting_setup
from services.settings import SettingsKey
from ui_qt.dialogs import meeting_setup as ui


@pytest.mark.parametrize("cached", [False, True])
def test_inspect_models_checks_selected_meeting_runtime(monkeypatch, cached):
    from services import components, hf_access
    from services.local_asr import catalog

    monkeypatch.setattr(meeting_setup.settings_manager, "load_all_settings", lambda: {
        SettingsKey.MEETING_SPEECH_SEPARATE: True,
        SettingsKey.MEETING_ASR_MODEL: "parakeet-v3",
        SettingsKey.MEETING_SPEAKER_ID_BACKEND: "off",
    })
    resolve = Mock(return_value=("asr-nvidia-cpu", "cpu"))
    monkeypatch.setattr(catalog, "resolve_runtime", resolve)
    monkeypatch.setattr(components, "is_installed", lambda _runtime: cached)
    monkeypatch.setattr(hf_access, "is_model_cached", lambda _model: cached)
    plan = meeting_setup.inspect_models()
    assert plan.runtime == ("" if cached else "asr-nvidia-cpu")
    assert plan.speech_download == ("" if cached else "parakeet-v3")
    assert not plan.speaker_download
    resolve.assert_called_once_with("parakeet", "auto")


def test_inspect_remote_does_not_require_local_speech_models(monkeypatch):
    from services import components, hf_access

    monkeypatch.setattr(meeting_setup.settings_manager, "load_all_settings", lambda: {
        SettingsKey.MEETING_SPEECH_SEPARATE: True,
        SettingsKey.MEETING_ASR_SOURCE: "remote",
    })
    monkeypatch.setattr(components, "speaker_model_path", lambda: "speaker.onnx")
    cached = Mock()
    monkeypatch.setattr(hf_access, "is_model_cached", cached)
    assert meeting_setup.inspect_models().missing == []
    cached.assert_not_called()


def test_prepare_installs_only_missing_files_and_releases_claims(monkeypatch):
    from services import components, hf_access

    plan = meeting_setup.ModelSetup("Parakeet", "parakeet-v3", "asr-nvidia-cpu", True)
    ready = meeting_setup.ModelSetup("Parakeet")
    monkeypatch.setattr(meeting_setup, "inspect_models", Mock(side_effect=[plan, ready]))
    monkeypatch.setattr(meeting_setup.settings_manager, "load_hf_access_policy", lambda: "ask")
    monkeypatch.setattr(components, "speaker_model_download_blocked", lambda: "")
    monkeypatch.setattr(components, "check_compatibility", lambda _entry: "")
    monkeypatch.setattr(components, "is_installed", lambda _component: False)
    monkeypatch.setattr(hf_access, "is_model_cached", lambda _model: False)
    coordinator = components.ComponentCoordinator()
    monkeypatch.setattr(coordinator, "catalog_entry", lambda _component: {})
    monkeypatch.setattr(components, "component_coordinator", coordinator)
    access = hf_access.HuggingFaceAccessCoordinator()
    monkeypatch.setattr(hf_access, "hf_access_coordinator", access)
    download, install, speaker = Mock(), Mock(), Mock()
    monkeypatch.setattr(hf_access, "download_model_files", download)
    monkeypatch.setattr(components, "install_component", install)
    monkeypatch.setattr(components, "ensure_speaker_model", speaker)

    cancel = threading.Event()
    assert meeting_setup.prepare_models(cancel, Mock()) == ready
    download.assert_called_once_with("parakeet-v3", cancel=cancel)
    assert install.call_args.args[0] == "asr-nvidia-cpu"
    assert install.call_args.args[-1] is cancel
    speaker.assert_called_once_with()
    assert not coordinator.is_any_installing()
    assert access.requests_in_flight == 0


def test_prepare_honors_offline_policy(monkeypatch):
    from services import hf_access

    plan = meeting_setup.ModelSetup("Whisper base", "base")
    monkeypatch.setattr(meeting_setup, "inspect_models", lambda: plan)
    monkeypatch.setattr(meeting_setup.settings_manager, "load_hf_access_policy", lambda: "never")
    download = Mock()
    monkeypatch.setattr(hf_access, "download_model_files", download)

    with pytest.raises(RuntimeError, match="downloads are disabled"):
        meeting_setup.prepare_models(threading.Event(), Mock())
    download.assert_not_called()


@pytest.mark.parametrize("platform,backend", [
    ("win32", "wasapi"), ("win32", "soundcard"), ("darwin", "sck"), ("linux", "soundcard"),
])
@pytest.mark.parametrize("system_level,ready", [(1000, True), (100, False), (0, False)])
def test_audio_check_routes_and_closes_both_devices(monkeypatch, platform, backend, system_level, ready):
    from meeting.capture import devices, sck_stream, sd_stream, soundcard_stream
    from services import audio_devices

    monkeypatch.setattr(meeting_setup, "sys", SimpleNamespace(platform=platform))
    device = {"index": 0, "samplerate": 16000, "channels": 1}
    monkeypatch.setattr(audio_devices, "load_priority", lambda: [])
    monkeypatch.setattr(devices, "find_mic_device", lambda _priority: device)
    find_loopback = Mock(return_value=device if backend == "wasapi" else None)
    monkeypatch.setattr(devices, "find_loopback_device", find_loopback)
    stopped = []

    class Source:
        def __init__(self, channel, *_format):
            self.channel = channel

        def start(self, observe):
            level = 100 if self.channel == CHANNEL_MIC else system_level
            frames = np.full(4000, level, dtype=np.int16)
            for _ in range(2):
                observe(CaptureBlock(self.channel, frames, 16000, 0))

        def is_active(self):
            return True

        def stop(self):
            stopped.append(self.channel)

    monkeypatch.setattr(sd_stream, "SdCaptureSource", Source)
    sck = Mock(return_value=Source(CHANNEL_LOOPBACK))
    sck.available.return_value = True
    soundcard = Mock(return_value=Source(CHANNEL_LOOPBACK))
    monkeypatch.setattr(sck_stream, "ScreenCaptureKitLoopbackSource", sck)
    monkeypatch.setattr(soundcard_stream, "SoundcardLoopbackSource", soundcard)
    result = meeting_setup.check_audio(threading.Event(), seconds=0)
    assert result.ready is ready
    assert result.microphone == "Signal detected"
    assert result.system_audio == ("Signal detected" if ready else "No signal detected")
    assert stopped == [CHANNEL_LOOPBACK, CHANNEL_MIC]
    assert find_loopback.called is (platform == "win32")
    assert sck.called is (backend == "sck")
    assert soundcard.called is (backend == "soundcard")


@pytest.mark.parametrize("saved,complete", [({}, False), ({SettingsKey.MEETING_SETUP_COMPLETE: True}, True)])
def test_setup_completion(monkeypatch, saved, complete):
    monkeypatch.setattr(meeting_setup.settings_manager, "load_all_settings", lambda: saved)
    assert meeting_setup.setup_complete() is complete


@pytest.mark.parametrize("mode", ["classic", "omarchy"])
def test_dialog_persists_only_completed_setup(monkeypatch, mode):
    monkeypatch.setenv("OPENWHISPER_UI", mode)
    monkeypatch.setattr(ui, "inspect_models", lambda: meeting_setup.ModelSetup("Parakeet"))
    monkeypatch.setattr(ui, "system_audio_permission_required", lambda: False)
    check = Mock(side_effect=[
        meeting_setup.AudioCheck("Signal detected", "No signal detected"),
        meeting_setup.AudioCheck("Signal detected", "Signal detected"),
    ])
    monkeypatch.setattr(ui, "check_audio", check)
    save = Mock()
    monkeypatch.setattr(ui.settings_manager, "save_setting", save)
    dialog = ui.MeetingSetupDialog(starting=True)

    def wait_for_check():
        for _ in range(100):
            if dialog.audio_button.isEnabled():
                return
            QTest.qWait(10)
        pytest.fail("Meeting setup worker did not finish")

    wait_for_check()
    assert not dialog.finish_button.isEnabled()
    assert not dialog.skip_button.isEnabled()
    dialog.audio_button.click()
    wait_for_check()
    assert dialog.skip_button.isEnabled()
    assert not dialog.finish_button.isEnabled()
    save.assert_not_called()
    dialog.audio_button.click()
    wait_for_check()
    assert dialog.finish_button.isEnabled()
    dialog.finish_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    save.assert_called_once_with(SettingsKey.MEETING_SETUP_COMPLETE, True)


def test_closing_waits_for_audio_devices_to_be_released(monkeypatch):
    monkeypatch.setattr(ui, "inspect_models", lambda: meeting_setup.ModelSetup("Parakeet"))
    monkeypatch.setattr(ui, "system_audio_permission_required", lambda: False)
    release = threading.Event()
    started = threading.Event()

    def check(cancel, **_kwargs):
        started.set()
        assert release.wait(3)
        assert cancel.is_set()
        return meeting_setup.AudioCheck("Signal detected", "Signal detected")

    monkeypatch.setattr(ui, "check_audio", check)
    dialog = ui.MeetingSetupDialog(starting=True)
    finished = Mock()
    dialog.finished.connect(finished)
    dialog.show()
    try:
        for _ in range(300):
            if dialog.audio_button.isEnabled():
                break
            QTest.qWait(10)
        assert dialog.audio_button.isEnabled()
        dialog.audio_button.click()
        assert started.wait(1)
        dialog.close()
        assert dialog.isVisible()
        assert not dialog.isEnabled()
        finished.assert_not_called()
    finally:
        release.set()
    for _ in range(300):
        if not dialog.isVisible():
            break
        QTest.qWait(10)
    assert not dialog.isVisible()
    finished.assert_called_once_with(QDialog.DialogCode.Rejected)


def test_cancelled_audio_check_does_not_open_devices(monkeypatch):
    from meeting.capture import devices

    find = Mock()
    monkeypatch.setattr(devices, "find_mic_device", find)
    cancel = threading.Event()
    cancel.set()
    meeting_setup.check_audio(cancel, seconds=0)
    find.assert_not_called()


def _wait_until_idle(dialog, timeout_ms=5000):
    """Wait for the dialog's background step (the model check it starts when
    it opens, or an audio check) to finish and its result to be shown. Check
    audio is disabled while one runs, so a fixed short wait can click it too
    early on a busy machine and the click is ignored."""
    waited = 0
    while not dialog.audio_button.isEnabled() and waited < timeout_ms:
        QTest.qWait(10)
        waited += 10
    assert dialog.audio_button.isEnabled()


@pytest.mark.usefixtures("isolated_app_theme")
@pytest.mark.parametrize("mode", ["classic", "omarchy"])
@pytest.mark.parametrize("font_percent", [100, 130])
def test_setup_fits_narrow_windows(monkeypatch, tmp_path, mode, font_percent):
    from ui_qt.utils.font_scale import apply_ui_font_scale
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", mode)
    monkeypatch.setattr(ui, "inspect_models", lambda: meeting_setup.ModelSetup("Parakeet"))
    apply_ui_font_scale(font_percent, app=QApplication.instance(), theme_manager=ThemeManager())
    dialog = ui.MeetingSetupDialog(starting=True)
    try:
        dialog.show()
        dialog.resize(460, dialog.sizeHint().height())
        _wait_until_idle(dialog)
        QTest.qWait(50)  # let the layout follow the model check's text
        assert dialog.width() <= 460
        for button in dialog.findChildren(QAbstractButton):
            if button.isVisible():
                assert dialog.rect().contains(button.mapTo(dialog, button.rect().topLeft()))
                assert dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
                assert button.width() >= button.minimumSizeHint().width()
        for label in dialog.findChildren(ui.WrappedLabel):
            if label.isVisible():
                assert label.height() >= label.heightForWidth(label.width())
        assert dialog.grab().save(str(tmp_path / f"setup-{mode}-{font_percent}.png"))
    finally:
        dialog.close()


def test_macos_permission_denial_still_checks_mic(monkeypatch):
    monkeypatch.setattr(ui, "inspect_models", lambda: meeting_setup.ModelSetup("Parakeet"))
    monkeypatch.setattr(ui, "system_audio_permission_required", lambda: True)
    monkeypatch.setattr(ui, "system_audio_permission_granted", lambda: False)
    request = Mock(return_value=False)
    monkeypatch.setattr(ui, "request_system_audio_permission", request)
    check = Mock(return_value=meeting_setup.AudioCheck("Signal detected", "Permission needed"))
    monkeypatch.setattr(ui, "check_audio", check)
    dialog = ui.MeetingSetupDialog(starting=True)
    request.assert_not_called()
    _wait_until_idle(dialog)
    dialog.audio_button.click()
    _wait_until_idle(dialog)
    request.assert_called_once_with()
    assert check.call_args.kwargs["system_audio_allowed"] is False
    assert not dialog.audio_settings.isHidden()
    assert dialog.skip_button.text() == "Start with microphone only"
    assert dialog.system_audio_policy == "disabled"
    assert not dialog.finish_button.isEnabled()


@pytest.mark.parametrize("platform,complete,setup_policy,policy", [
    ("win32", False, None, None),
    ("win32", False, "auto", "auto"),
    ("win32", True, None, "auto"),
    ("darwin", False, "disabled", "disabled"),
    ("darwin", False, "auto", "auto"),
    ("linux", False, "required", "required"),
])
def test_first_start_runs_shared_setup(monkeypatch, platform, complete, setup_policy, policy):
    from ui_qt.ui_controller import UIController

    ctrl = SimpleNamespace(
        main_window=SimpleNamespace(tabbed_content=SimpleNamespace(unlock_meeting_tab=Mock())),
        _meeting_setup_open=False,
        _open_meeting_setup=Mock(return_value=setup_policy),
    )
    monkeypatch.setattr(
        "ui_qt.dialogs.meeting_unsupported_dialog.acknowledge_unsupported_meeting_mode",
        lambda _window: True,
    )
    monkeypatch.setattr(
        "ui_qt.dialogs.meeting_system_audio_dialog.ensure_meeting_system_audio_permission",
        lambda _window: True,
    )
    monkeypatch.setattr(meeting_setup, "setup_complete", lambda: complete)
    monkeypatch.setattr("meeting.platform.meeting_setup_available", lambda: True)
    monkeypatch.setattr("sys.platform", platform)
    assert UIController.ensure_meeting_start_readiness(ctrl) == policy
    if complete:
        ctrl._open_meeting_setup.assert_not_called()
    else:
        ctrl._open_meeting_setup.assert_called_once_with(starting=True)
