"""
Services package - core business logic and managers.
"""
from services.recorder import AudioRecorder
from services.audio_processor import audio_processor, AudioProcessor, AudioFilePreview
from services.hotkey_manager import HotkeyManager
from services.history_manager import history_manager, HistoryManager, HistoryEntry, RecordingInfo
from services.settings import settings_manager, SettingsManager

__all__ = [
    'AudioRecorder',
    'audio_processor',
    'AudioProcessor',
    'AudioFilePreview',
    'HotkeyManager',
    'history_manager',
    'HistoryManager',
    'HistoryEntry',
    'RecordingInfo',
    'settings_manager',
    'SettingsManager',
]
