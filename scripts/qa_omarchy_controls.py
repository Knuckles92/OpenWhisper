"""Exercise native D-Bus actions, binding lifecycle, and Wayland paste without audio."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["OPENWHISPER_DATA_DIR"] = tempfile.mkdtemp(prefix="ow-controls-qa-")

from PyQt6.QtCore import QMimeData
from PyQt6.QtWidgets import QApplication, QLineEdit, QWidget

from services import hyprland
from services._hotkey_pynput import HotkeyManager
from services.omarchy_controls import INTERFACE, PATH, SERVICE, OmarchyControls

app = QApplication([])
app.setDesktopFileName("openwhisper-controls-qa")
target = QLineEdit()
target.setWindowTitle("OpenWhisper native controls test")
target.resize(620, 100)
target.show()
manager = HotkeyManager({"record_toggle": "ctrl+alt+f24", "cancel": "ctrl+alt+f23"})
pressed = []
manager.set_callbacks(
    on_record_toggle=lambda: pressed.append("record"),
    on_cancel=lambda: pressed.append("cancel"),
)
controller = SimpleNamespace(
    hotkey_manager=manager,
    recorder=SimpleNamespace(is_recording=False),
    is_transcribing=lambda: False,
    is_meeting_active=lambda: False,
    ui_controller=SimpleNamespace(
        main_window=QWidget(),
        _on_tray_toggle_recording=lambda: pressed.append("record-button"),
    ),
)
native = OmarchyControls(controller)
errors = []
checks = []
clipboard = app.clipboard()
previous = QMimeData()
for mime in clipboard.mimeData().formats():
    previous.setData(mime, clipboard.mimeData().data(mime))


def settle(seconds=0.4):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def command(args):
    result = []

    def worker():
        try:
            result.append(subprocess.check_output(args, text=True, timeout=5))
        except Exception as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=worker)
    thread.start()
    while thread.is_alive():
        settle(0.02)
    assert not errors, errors
    return result[0]


try:
    assert native.start()
    settle(1)
    assert "record_toggle" in native._owned, native._status
    checks.append("compositor bindings registered")
    for action in ("record_toggle", "cancel"):
        reply = command(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                SERVICE,
                "--object-path",
                PATH,
                "--method",
                INTERFACE + ".Trigger",
                action,
                "false",
            ]
        )
        assert "true" in reply
    settle()
    assert pressed == ["record", "cancel"], pressed
    checks.append(
        "D-Bus dispatch reaches callbacks with application main window unfocused"
    )
    manager.program_enabled = False
    reply = command(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            SERVICE,
            "--object-path",
            PATH,
            "--method",
            INTERFACE + ".Button",
            "record",
        ]
    )
    assert "true" in reply and pressed[-1] == "record-button"
    manager.program_enabled = True
    checks.append(
        "shell click uses app button action while keyboard shortcuts are paused"
    )
    manager.set_capture_suspended(True)
    native.refresh()
    settle(0.5)
    assert not native._owned
    checks.append("shortcut capture releases compositor bindings")
    manager.set_capture_suspended(False)
    native.refresh()
    settle(0.5)
    assert "record_toggle" in native._owned
    checks.append("shortcuts restored after capture")
    controller.recorder.is_recording = True
    controller.ui_controller.overlay = SimpleNamespace(
        _streaming_preview_text="Native Omarchy recording preview. This is a UI test; no microphone is running."
    )
    native._write_state()
    settle(1)
    subprocess.run(
        ["grim", "-g", "600,0 680x320", "/tmp/openwhisper-omarchy-shell-preview.png"],
        check=True,
        timeout=5,
    )
    controller.recorder.is_recording = False
    controller.ui_controller.overlay._streaming_preview_text = ""
    native._write_state()
    client = next(c for c in hyprland.query("clients") if c["pid"] == os.getpid())
    hyprland.evaluate(
        "hl.dispatch(hl.dsp.focus({window="
        + json.dumps("address:" + client["address"])
        + "}))"
    )
    settle()
    clipboard.setText("OpenWhisper native paste test")
    settle()
    hyprland.send_paste()
    settle()
    assert target.text() == "OpenWhisper native paste test", target.text()
    checks.append("Hyprland pasted the clipboard into a native Wayland field")
    native.close()
    settle()
    assert not any(
        b.get("description", "").startswith("OpenWhisper: ")
        and b.get("key", "").upper() in ("F23", "F24")
        and b.get("modmask") == 12
        for b in hyprland.query("binds")
    )
    checks.append("probe bindings removed on shutdown")
    print(json.dumps({"checks": checks}, indent=2), flush=True)
    code = 0
except Exception:
    import traceback

    traceback.print_exc()
    code = 1
finally:
    native.close()
    manager.cleanup()
    clipboard.setMimeData(previous)
    settle()
    target.close()
sys.stdout.flush()
sys.stderr.flush()
os._exit(code)
