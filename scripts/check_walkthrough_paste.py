"""Manual desktop smoke test for the dedicated PDF + instructions shortcut.

Run from the project root. Click a chat composer, press Ctrl+Shift+F12 once,
and verify that it contains one PDF and the instructions, without being sent.
This uses the production hotkey backend, release wait, focus checks and paste
coordinator. It registers only this shortcut and exits after four minutes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QObject, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from services.focus_context import get_service, shutdown_service  # noqa: E402
from services.hotkey_manager import HotkeyManager, send_paste  # noqa: E402
from services.runtime.hotkeys import HotkeyRuntime  # noqa: E402
from services.walkthrough.library import load_result  # noqa: E402
from ui_qt.clipboard import TemporaryClipboard  # noqa: E402
from ui_qt.walkthrough_paste import WalkthroughPaste, latest_result  # noqa: E402


class Bridge(QObject):
    paste_walkthrough_requested = pyqtSignal(object)
    status_update = pyqtSignal(str)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--shortcut", default="ctrl+shift+f12")
    parser.add_argument("--diagnose", action="store_true", help="Log only the shortcut's main-key events")
    args = parser.parse_args()
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    result = load_result(args.folder) if args.folder else latest_result()
    if result is None:
        raise SystemExit("No completed walkthrough")
    bridge = Bridge()
    bridge.status_update.connect(lambda text: print(text, flush=True))
    writer = TemporaryClipboard(app.clipboard())
    paste = WalkthroughPaste(
        app.clipboard(), writer, identity=lambda: get_service().current_identity(),
        send_paste=send_paste, report=bridge.status_update.emit,
    )
    bridge.paste_walkthrough_requested.connect(lambda target: paste.start(result, target))
    runtime = HotkeyRuntime(SimpleNamespace(
        paste_walkthrough_requested=bridge.paste_walkthrough_requested,
        status_update=bridge.status_update,
    ))
    manager_type = HotkeyManager
    if args.diagnose and sys.platform == "win32":
        import keyboard

        class DiagnosticManager(HotkeyManager):
            def _handle_keyboard_event(self, event):
                if event.name == args.shortcut.split("+")[-1]:
                    modifiers = [key for key in ("ctrl", "alt", "shift", "win") if keyboard.is_pressed(key)]
                    from services.synthetic_keys import native_windows_modifiers
                    native = sorted(native_windows_modifiers() or ())
                    print(f"Shortcut key: {event.event_type}; scan: {event.scan_code}; modifiers: {modifiers}; OS: {native}", flush=True)
                return super()._handle_keyboard_event(event)

        manager_type = DiagnosticManager
    manager = manager_type({"paste_walkthrough": args.shortcut})
    manager.set_callbacks(on_paste_walkthrough=runtime._paste_walkthrough_after_keys_up)
    QTimer.singleShot(240_000, app.quit)
    print(f"Ready: {args.shortcut}; PDF: {result.pdf}", flush=True)
    try:
        app.exec()
    finally:
        paste.cancel()
        manager.cleanup()
        writer.cleanup()
        shutdown_service()


if __name__ == "__main__":
    main()
