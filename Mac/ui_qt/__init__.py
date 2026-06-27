"""PyQt6 UI package for OpenWhisper."""

from importlib import import_module

__all__ = [
    "QtApplication",
    "MainWindow",
    "LoadingScreen",
    "WaveformOverlay",
]

_EXPORT_MAP = {
    "QtApplication": ("ui_qt.app", "QtApplication"),
    "MainWindow": ("ui_qt.main_window", "MainWindow"),
    "LoadingScreen": ("ui_qt.loading_screen", "LoadingScreen"),
    "WaveformOverlay": ("ui_qt.overlays.waveform_overlay", "WaveformOverlay"),
}


def __getattr__(name):
    if name not in _EXPORT_MAP:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute_name = _EXPORT_MAP[name]
    module = import_module(module_name)
    value = getattr(module, attribute_name)
    globals()[name] = value
    return value
