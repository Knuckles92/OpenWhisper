"""Small runtime checks used by the frozen application's --self-test."""

import importlib
import multiprocessing
import sys

#: App modules reached only from inside functions or by module name. Freeze
#: analysis finds the first kind only while the import stays a literal
#: statement, so the self-test imports each to prove the build shipped it.
ON_DEMAND_MODULES = (
    "services.audio_devices",
    "services.audio_player",
    "services.dictation_stats",
    "services.focus_context._capture",
    "services.synthetic_keys",
    "services.text_rewrite",
    "services.vocabulary",
    "ui_qt.dialogs.cleanup_levels",
    "ui_qt.dialogs.settings_microphones",
    "ui_qt.dialogs.stats_dialog",
    "ui_qt.history_actions",
    "ui_qt.widgets.command_settings",
    "ui_qt.widgets.dictionary_library",
    "ui_qt.widgets.history_playback",
    "ui_qt.widgets.hotkey_row",
    "ui_qt.widgets.language_menu",
    "ui_qt.widgets.scratchpad",
    "ui_qt.widgets.snippets_panel",
)
_PLATFORM_MODULES = {
    "win32": (
        "services._mouse_hook_win",
        "services.focus_context._win",
        "services.focus_context._win_uia",
    ),
    "darwin": ("services.focus_context._mac",),
    "linux": ("services.focus_context._linux",),
}


def app_module_names(platform: str = sys.platform) -> list[str]:
    """Return the app modules the self-test imports on ``platform``."""
    from ui_qt.dialogs.settings_dialog import _PAGE_MODULES

    key = platform if platform in ("win32", "darwin") else "linux"
    return [
        *ON_DEMAND_MODULES,
        *_PLATFORM_MODULES[key],
        *(path for _key, path, _icon in _PAGE_MODULES),
    ]


def import_app_modules() -> None:
    """Import every lazily loaded app module and public UI export."""
    from ui_qt import dialogs, widgets

    for package in (widgets, dialogs):
        for name in package.__all__:
            getattr(package, name)
    for name in app_module_names():
        importlib.import_module(name)


def _multiprocessing_probe(connection, lock):
    """A spawned worker must never run the application bootstrap."""
    try:
        with lock:
            connection.send([
                name for name in ("config", "ui_qt.bootstrap", "services.application_controller")
                if name in sys.modules
            ])
    finally:
        connection.close()


def check_multiprocessing() -> None:
    """Exercise spawn and the resource tracker without opening an app window."""
    context = multiprocessing.get_context("spawn")
    # A spawn-context lock starts the POSIX resource tracker, including the
    # same path used by tqdm during Whisper's first transcription.
    lock = context.RLock()
    reader, writer = context.Pipe(duplex=False)
    worker = context.Process(target=_multiprocessing_probe, args=(writer, lock))
    try:
        worker.start()
        writer.close()
        if not reader.poll(20):
            raise RuntimeError("Multiprocessing worker did not respond")
        imported = reader.recv()
        worker.join(timeout=5)
        if worker.exitcode != 0:
            raise RuntimeError(f"Multiprocessing worker did not exit cleanly: {worker.exitcode}")
        if imported:
            raise RuntimeError(f"Multiprocessing worker initialized the app: {imported}")
    finally:
        reader.close()
        writer.close()
        if worker.pid is not None:
            if worker.is_alive():
                worker.kill()
            worker.join(timeout=5)
            worker.close()
