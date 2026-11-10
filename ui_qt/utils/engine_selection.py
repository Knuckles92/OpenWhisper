"""Tells every engine field when the saved dictation engine changes.

Quick Record, Upload File and Settings each show the engine: its backend,
model, device and language. They are views of the saved settings, so each
listens here and reloads, whoever made the change: another view, the
controller reverting a device, or a paired computer switching this one's
model.
"""
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from services.settings import SettingsKey, settings_manager

#: What the engine fields show. A save that changes any of these is heard.
ENGINE_KEYS = (
    SettingsKey.SELECTED_MODEL,
    SettingsKey.WHISPER_MODEL,
    SettingsKey.WHISPER_DEVICE,
    SettingsKey.WHISPER_COMPUTE_TYPE,
    SettingsKey.CUSTOM_WHISPER_MODELS,
    SettingsKey.LOCAL_ASR_MODELS,
    SettingsKey.LOCAL_ASR_DEVICES,
    SettingsKey.LOCAL_ASR_LANGUAGE,
    SettingsKey.API_TRANSCRIPTION_ENDPOINT,
    SettingsKey.API_TRANSCRIPTION_SERVERS,
    SettingsKey.API_TRANSCRIPTION_MODEL,
    # The Language field steps aside while a dictation language is in charge.
    SettingsKey.DICTATION_LANGUAGES,
    SettingsKey.DICTATION_ACTIVE_LANGUAGE,
)


class _EngineSelectionRelay(QObject):
    """Brings a settings save, made on any thread, to the views' Qt thread.

    It lives for the session, so the settings watch never holds a view; Qt
    drops a deleted view's connection.
    """

    changed = pyqtSignal()


_relay: Optional[_EngineSelectionRelay] = None


def engine_selection_relay() -> _EngineSelectionRelay:
    """The session's relay, built on first use by a view on the Qt thread."""
    global _relay
    if _relay is None:
        relay = _relay = _EngineSelectionRelay()
        settings_manager.watch(ENGINE_KEYS, lambda _changed: relay.changed.emit())
    return _relay
