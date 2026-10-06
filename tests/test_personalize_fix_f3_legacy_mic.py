"""The old microphone index migrates once it names a microphone, never as "none".

A Bluetooth or USB microphone may not be connected yet when the first
upgraded launch runs, for example when the app starts at login.
"""
import threading

from services import audio_devices
from services.audio_devices import load_priority
from services.recorder import AudioRecorder
from services.settings import SettingsKey, SettingsManager, settings_manager
from tests.test_personalize_s7_audio_devices import MME, windows_sd
from tests.test_personalize_s7_recorder import _wait, fake_sd  # noqa: F401  (fixtures)

USB = {"name": "Microphone (2- USB Audio Device", "hostapi": MME}


def _unplugged(sd):
    """``sd`` without the USB microphone at index 2."""
    sd.devices = sd.devices[:2]
    return sd


def test_an_unplugged_legacy_microphone_is_migrated_once_it_is_back(tmp_path):
    store = SettingsManager(str(tmp_path / "settings.json"))
    store.save_all_settings({SettingsKey.AUDIO_INPUT_DEVICE: 2})

    assert load_priority(manager=store, sd=_unplugged(windows_sd())) == []
    assert SettingsKey.AUDIO_INPUT_PRIORITY not in store.load_all_settings()

    assert load_priority(manager=store, sd=windows_sd()) == [USB]
    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [USB]
    assert store.get(SettingsKey.AUDIO_INPUT_DEVICE) == 2


def test_from_settings_never_lists_devices_on_the_calling_thread(fake_sd, monkeypatch):  # noqa: F811
    settings_manager.update_settings({SettingsKey.AUDIO_INPUT_DEVICE: 2})
    callers = []
    listed = fake_sd.query_devices
    monkeypatch.setattr(fake_sd, "query_devices",
                        lambda: callers.append(threading.current_thread()) or listed())

    recorder = AudioRecorder.from_settings()
    try:
        assert _wait(lambda: recorder.device_priority == [USB])
        assert settings_manager.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [USB]
        assert callers and threading.current_thread() not in callers
    finally:
        recorder.cleanup()


def test_a_recorder_built_while_the_microphone_is_unplugged_keeps_trying(fake_sd, monkeypatch):  # noqa: F811
    settings_manager.update_settings({SettingsKey.AUDIO_INPUT_DEVICE: 2})
    finished = threading.Event()
    migrate = audio_devices.load_priority

    def tracked(*args, **kwargs):
        try:
            return migrate(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(audio_devices, "load_priority", tracked)
    plugged = fake_sd.devices
    _unplugged(fake_sd)

    first = AudioRecorder.from_settings()
    assert finished.wait(3)
    first.cleanup()
    assert first.device_priority == []
    assert SettingsKey.AUDIO_INPUT_PRIORITY not in settings_manager.load_all_settings()

    fake_sd.devices = plugged
    second = AudioRecorder.from_settings()
    try:
        assert _wait(lambda: second.device_priority == [USB])
        assert settings_manager.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [USB]
    finally:
        second.cleanup()
