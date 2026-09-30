"""Exercise the actual Qt/Hyprland surface without starting engines or recording.

Run with the desktop session environment. Writes widget captures and a JSON
report to --output; settings/history live in a disposable directory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_keepalive = []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--extended",
        action="store_true",
        help="Exercise windows, settings, popups, shortcuts, and font/theme transitions",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openwhisper-ui-qa-") as data_dir:
        os.environ["OPENWHISPER_DATA_DIR"] = data_dir
        os.environ["OPENWHISPER_UI"] = "omarchy"
        from PyQt6.QtCore import qInstallMessageHandler

        from services.settings import SettingsKey, settings_manager
        from ui_qt.app import QtApplication
        from ui_qt.dialogs.settings_dialog import SettingsDialog
        from ui_qt.loading_screen import LoadingScreen
        from ui_qt.main_window import MainWindow
        from ui_qt.utils.palette import current_palette
        from ui_qt.widgets.buttons import HotkeyHintFilter

        messages = []
        qInstallMessageHandler(lambda _type, _context, text: messages.append(text))
        settings_manager.save_setting(SettingsKey.MEETING_MODE_INTRO_SEEN, True)
        start = time.monotonic()
        runtime = QtApplication()
        _keepalive.append(runtime)
        app = runtime.app
        app.setQuitOnLastWindowClosed(False)
        # Distinct app ID so the probe cannot take over the installed launcher.
        app.setDesktopFileName("openwhisper-ui-qa")

        def settle(seconds=0.8):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                app.processEvents()
                time.sleep(0.01)

        splash = LoadingScreen()
        splash.show()
        settle()
        splash.grab().save(str(args.output / "splash.png"))
        splash.destroy()
        window = MainWindow()
        window.setWindowTitle("OpenWhisper — Omarchy UI test")
        window.show()
        settle()
        report = {
            "platform": app.platformName(),
            "dpr": window.devicePixelRatioF(),
            "ui_seconds": round(time.monotonic() - start, 3),
            "palette": {k: current_palette().css(k) for k in ("bg", "text", "accent")},
            "views": [],
        }

        def capture(name, target=None):
            target = target or window
            print(f"Capturing {name}", flush=True)
            settle()
            view = {
                "name": name,
                "qt_size": [target.width(), target.height()],
                "content_size": [
                    target.contentsRect().width(),
                    target.contentsRect().height(),
                ],
            }
            if app.platformName().startswith("wayland"):
                clients = json.loads(
                    subprocess.check_output(
                        ["hyprctl", "-j", "clients"], text=True, timeout=5
                    )
                )
                candidates = [
                    c
                    for c in clients
                    if c["pid"] == os.getpid() or c["class"] == "openwhisper-ui-qa"
                ]
                assert candidates, {
                    "pid": os.getpid(),
                    "visible": window.isVisible(),
                    "title": window.windowTitle(),
                    "candidates": [
                        (c["pid"], c["class"], c["size"])
                        for c in clients
                        if "whisper" in c["class"].lower()
                    ],
                }
                client = next(
                    (
                        c
                        for c in candidates
                        if c["title"]
                        in (
                            target.windowTitle(),
                            target.windowTitle() + " — OpenWhisper",
                        )
                    ),
                    None,
                )
                assert client is not None, (name, target.windowTitle(), candidates)
                view.update(
                    compositor_size=client["size"],
                    at=client["at"],
                    xwayland=client["xwayland"],
                )
                assert view["qt_size"] == client["size"], view
                assert client["at"][1] >= 0, view
            assert view["qt_size"] == view["content_size"], view
            target.grab().save(str(args.output / f"{name}.png"))
            report["views"].append(view)
            (args.output / "report.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )

        capture("record")
        window.set_host_mode(True, persist=False)
        capture("host")
        window.set_host_mode(False, persist=False)
        window.set_compact_mode(True, persist=False)
        capture("compact")
        window.set_compact_mode(False, persist=False)
        window.toggle_history()
        capture("history")
        window.toggle_history()
        window.tabbed_content.set_current_index(1)
        capture("upload")
        window.tabbed_content.set_current_index(2)
        capture("meeting")
        if app.platformName().startswith("wayland"):
            from PyQt6.QtWidgets import QWidget

            sibling = QWidget()
            sibling.setWindowTitle("OpenWhisper UI test — tile constraint")
            sibling.show()
            capture("small-tile")
            sibling.close()
            settle()
        hint = HotkeyHintFilter(window.quit_button, "Ctrl+Q")
        print("Showing native shortcut hint", flush=True)
        hint.show_hint()
        settle()
        hint.hide_hint()
        dialog = SettingsDialog(
            window, get_loaded_model=lambda: None, background_cache_scan=False
        )
        print("Showing settings", flush=True)
        dialog.show()
        settle()
        dialog.grab().save(str(args.output / "settings.png"))
        report["settings_size"] = [dialog.width(), dialog.height()]
        dialog.close()
        if args.extended:
            from qa_omarchy_matrix import exercise_ui

            exercise_ui(runtime, window, dialog, settle, capture, report, _keepalive)
        window._force_quit = True
        window.close()
        settle(0.1)
        report["warnings"] = messages
        from services.database import db

        db.close()
        bad = [
            m
            for m in messages
            if any(
                s in m.lower()
                for s in ("opacity", "could not parse stylesheet", "traceback")
            )
        ]
        (args.output / "report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, indent=2))
        _keepalive.extend((window, dialog, splash, hint))
        assert not bad, bad
        return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback

        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    # Like pytest, avoid the known Qt/PortAudio native teardown race.
    os._exit(code)
