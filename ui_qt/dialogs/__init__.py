"""Public UI exports loaded only when a feature requests them."""
from importlib import import_module

_EXPORTS = {'SettingsDialog': ('ui_qt.dialogs.settings_dialog', 'SettingsDialog'), 'CleanupPromptDialog': ('ui_qt.dialogs.cleanup_prompt_dialog', 'CleanupPromptDialog'), 'CleanupRuleDialog': ('ui_qt.dialogs.cleanup_rule_dialog', 'CleanupRuleDialog'), 'BatchRelationDialog': ('ui_qt.dialogs.batch_relation_dialog', 'BatchRelationDialog'), 'HistoryEntryDialog': ('ui_qt.dialogs.history_entry_dialog', 'HistoryEntryDialog'), 'TranscriptViewerDialog': ('ui_qt.dialogs.transcript_viewer_dialog', 'TranscriptViewerDialog')}
__all__ = list(_EXPORTS)


def __getattr__(name):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module, attribute = target
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value
