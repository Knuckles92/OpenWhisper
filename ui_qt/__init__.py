"""PyQt6 UI package for OpenWhisper."""

from importlib import import_module

__all__ = [
    "QtApplication",
    "ModernMainWindow",
    "ModernLoadingScreen",
    "ModernWaveformOverlay",
]

_EXPORT_MAP = {
    "QtApplication": ("ui_qt.app", "QtApplication"),
    "ModernMainWindow": ("ui_qt.main_window_qt", "ModernMainWindow"),
    "ModernLoadingScreen": ("ui_qt.loading_screen_qt", "ModernLoadingScreen"),
    "ModernWaveformOverlay": ("ui_qt.overlay_qt", "ModernWaveformOverlay"),
}


def __getattr__(name):
    if name not in _EXPORT_MAP:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute_name = _EXPORT_MAP[name]
    module = import_module(module_name)
    value = getattr(module, attribute_name)
    globals()[name] = value
    return value
