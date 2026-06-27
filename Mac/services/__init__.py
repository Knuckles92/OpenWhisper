"""Services package - core business logic and managers."""

from importlib import import_module

__all__ = [
    "AudioRecorder",
    "audio_processor",
    "AudioProcessor",
    "AudioFilePreview",
    "HotkeyManager",
    "history_manager",
    "HistoryManager",
    "HistoryEntry",
    "RecordingInfo",
    "settings_manager",
    "SettingsManager",
]

_EXPORT_MAP = {
    "AudioRecorder": ("services.recorder", "AudioRecorder"),
    "audio_processor": ("services.audio_processor", "audio_processor"),
    "AudioProcessor": ("services.audio_processor", "AudioProcessor"),
    "AudioFilePreview": ("services.audio_processor", "AudioFilePreview"),
    "HotkeyManager": ("services.hotkey_manager", "HotkeyManager"),
    "history_manager": ("services.history_manager", "history_manager"),
    "HistoryManager": ("services.history_manager", "HistoryManager"),
    "HistoryEntry": ("services.history_manager", "HistoryEntry"),
    "RecordingInfo": ("services.history_manager", "RecordingInfo"),
    "settings_manager": ("services.settings", "settings_manager"),
    "SettingsManager": ("services.settings", "SettingsManager"),
}


def __getattr__(name):
    if name not in _EXPORT_MAP:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute_name = _EXPORT_MAP[name]
    module = import_module(module_name)
    value = getattr(module, attribute_name)
    globals()[name] = value
    return value
