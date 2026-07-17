"""
Dialog windows for PyQt6 UI.
"""
from ui_qt.dialogs.settings_dialog import SettingsDialog
from ui_qt.dialogs.hotkey_dialog import HotkeyDialog
from ui_qt.dialogs.model_manager_dialog import ModelManagerDialog
from ui_qt.dialogs.model_details_dialog import ModelDetailsDialog
from ui_qt.dialogs.cleanup_prompt_dialog import CleanupPromptDialog
from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog

__all__ = [
    "SettingsDialog",
    "HotkeyDialog",
    "ModelManagerDialog",
    "ModelDetailsDialog",
    "CleanupPromptDialog",
    "HistoryEntryDialog",
]
