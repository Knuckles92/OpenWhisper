"""Session-bus controls and compositor-owned shortcuts for Omarchy/Hyprland.

No keyboard hook, shell config edits, or UI-thread subprocesses. Runtime binds
are reconciled after compositor reloads and only claimed when the key is free.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection
from PyQt6.QtWidgets import QApplication

from services import hyprland
from services._hotkey_pynput import format_hotkey_display, parse_hotkey

log = logging.getLogger(__name__)
SERVICE = "org.openwhisper.OpenWhisper"
PATH = "/org/openwhisper/OpenWhisper"
INTERFACE = "org.openwhisper.Control"
PREFIX = "OpenWhisper: "
_MODS = {
    "shift": ("SHIFT", 1),
    "ctrl": ("CTRL", 4),
    "alt": ("ALT", 8),
    "cmd": ("SUPER", 64),
}
_KEYS = {
    "space": "space",
    "esc": "Escape",
    "enter": "Return",
    "tab": "Tab",
    "page_up": "Prior",
    "page_down": "Next",
    "backspace": "BackSpace",
    "delete": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
}
_KEYS.update(
    {
        "*": "asterisk",
        "-": "minus",
        "+": "plus",
        "/": "slash",
        ".": "period",
        ",": "comma",
        ";": "semicolon",
        "'": "apostrophe",
        "[": "bracketleft",
        "]": "bracketright",
        "\\": "backslash",
        "`": "grave",
        "=": "equal",
        "caps_lock": "Caps_Lock",
        "num_lock": "Num_Lock",
        "scroll_lock": "Scroll_Lock",
        "print_screen": "Print",
        "pause": "Pause",
        "menu": "Menu",
    }
)
_KEYPAD = {
    "*": "KP_Multiply",
    "-": "KP_Subtract",
    "+": "KP_Add",
    "/": "KP_Divide",
    ".": "KP_Decimal",
    "enter": "KP_Enter",
    **{str(n): f"KP_{n}" for n in range(10)},
}


def binding_key(hotkey: str) -> tuple[str, str, int]:
    modifiers, key = parse_hotkey(hotkey)
    if not key or not modifiers.issubset(_MODS):
        raise ValueError(f"Unsupported shortcut: {hotkey}")
    if key.startswith("kp "):
        symbol = _KEYPAD.get(key[3:])
    else:
        symbol = _KEYS.get(key)
        if symbol is None and re.fullmatch(r"[a-z0-9]|f(?:[1-9]|1[0-9]|2[0-4])", key):
            symbol = key.upper() if key.startswith("f") else key
    if symbol is None:
        raise ValueError(f"Unsupported shortcut: {hotkey}")
    names = [_MODS[m][0] for m in _MODS if m in modifiers]
    mask = sum(_MODS[m][1] for m in modifiers)
    return " + ".join([*names, symbol]), symbol, mask


def matches(bind: dict, symbol: str, mask: int) -> bool:
    return bind.get("key", "").lower() == symbol.lower() and bind.get("modmask") == mask


class OmarchyControls(QObject):
    status_changed = pyqtSignal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._owned = {}
        self._status = "Starting desktop shortcuts"
        self._active_hotkeys = []
        self.bus = QDBusConnection.sessionBus()
        self.status_changed.connect(self._publish_status)
        self._state_timer = QTimer(self)
        self._state_timer.setInterval(250)
        self._state_timer.timeout.connect(self._write_state)
        self._state_path = None
        self._last_state = None
        self._last_write = 0.0

    def _publish_status(self, status):
        app = QApplication.instance()
        if app is not None:
            app.setProperty("omarchyGlobalHotkeys", self._active_hotkeys)
            app.setProperty("omarchyShortcutStatus", status)
        manager = self.controller.hotkey_manager
        manager.backend_available = bool(self._active_hotkeys)
        manager.backend_name = "hyprland" if self._active_hotkeys else "unavailable"
        manager.backend_error = "" if status == "Desktop shortcuts active" else status

    def start(self) -> bool:
        if not hyprland.available() or not shutil.which("gdbus"):
            return False
        if not self.bus.registerService(SERVICE):
            return False
        if not self.bus.registerObject(
            PATH, INTERFACE, self, QDBusConnection.RegisterOption.ExportAllSlots
        ):
            self.bus.unregisterService(SERVICE)
            return False
        self._thread = threading.Thread(
            target=self._run, name="omarchy-controls", daemon=True
        )
        self._thread.start()
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_dir:
            try:
                folder = Path(runtime_dir) / "openwhisper"
                folder.mkdir(mode=0o700, exist_ok=True)
                self._state_path = folder / "status.json"
                self._state_timer.start()
            except OSError:
                log.exception("Could not initialize Omarchy bar status")
        return True

    @pyqtSlot(str, bool, result=bool)
    def Trigger(self, action: str, released: bool) -> bool:
        manager = self.controller.hotkey_manager
        if action == "show" and not released:
            self.controller.ui_controller.main_window.restore_from_tray()
            return True
        if action not in manager._all_hotkeys():
            return False
        manager.trigger_action(action, released=released)
        return True

    @pyqtSlot(str, result=bool)
    def Button(self, action: str) -> bool:
        """Shell clicks behave like app buttons, independent of keyboard mode."""
        ui = self.controller.ui_controller
        if action == "show":
            ui.main_window.restore_from_tray()
            return True
        method = {
            "record": "_on_tray_toggle_recording",
            "cancel": "cancel_recording",
            "meeting": "_on_tray_meeting_toggle",
        }.get(action)
        if method is None:
            return False
        getattr(ui, method)()
        return True

    @pyqtSlot(result=str)
    def Status(self) -> str:
        c = self.controller
        return json.dumps(
            {
                "recording": bool(c.recorder.is_recording),
                "transcribing": bool(c.is_transcribing()),
                "meeting": bool(c.is_meeting_active()),
                "enabled": bool(c.hotkey_manager.program_enabled),
                "shortcuts": self._status,
                "window_active": c.ui_controller.main_window.isActiveWindow(),
            }
        )

    def _write_state(self):
        if self._state_path is None:
            return
        try:
            data = json.loads(self.Status())
            overlay = getattr(self.controller.ui_controller, "overlay", None)
            data["preview"] = getattr(overlay, "_streaming_preview_text", "")[-1600:]
            data["available"] = not self._stop.is_set()
            now = time.time()
            if data == self._last_state and now - self._last_write < 3:
                return
            self._last_state = data.copy()
            self._last_write = now
            data["updated"] = int(now * 1000)
            temporary = self._state_path.with_suffix(".next")
            temporary.write_text(json.dumps(data), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self._state_path)
        except Exception:
            log.exception("Could not update Omarchy recording indicator")

    def refresh(self) -> None:
        self._wake.set()

    def _desired(self):
        manager = self.controller.hotkey_manager
        if self._stop.is_set() or manager.capture_suspended:
            return {}
        return {a: k for a, k in manager._all_hotkeys().items() if k}

    def _sync(self):
        desired = self._desired()
        binds = hyprland.query("binds")
        issues = []
        active = []
        for action, (hotkey, sequence, symbol, mask) in list(self._owned.items()):
            if desired.get(action) == hotkey:
                continue
            matching = [b for b in binds if matches(b, symbol, mask)]
            if matching and all(
                b.get("description", "").startswith(PREFIX) for b in matching
            ):
                hyprland.evaluate(f"hl.unbind({json.dumps(sequence)})")
                binds = [b for b in binds if not matches(b, symbol, mask)]
            del self._owned[action]
        for action, hotkey in desired.items():
            try:
                sequence, symbol, mask = binding_key(hotkey)
            except ValueError as exc:
                issues.append(str(exc))
                continue
            description = PREFIX + action
            matching = [b for b in binds if matches(b, symbol, mask)]
            if matching and any(b.get("description") != description for b in matching):
                issues.append(f"{hotkey} is already used by Hyprland")
                continue
            if matching and {bool(b.get("release")) for b in matching} != {False, True}:
                # Repair an interrupted registration before advertising it as active.
                hyprland.evaluate(f"hl.unbind({json.dumps(sequence)})")
                matching = []
            if not matching:
                self._owned[action] = (hotkey, sequence, symbol, mask)
                for released in (False, True):
                    # Actions are passed as a single shell-quoted gdbus argument.
                    # Profile IDs are data, never command fragments.
                    import shlex

                    command = shlex.join(
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
                            "true" if released else "false",
                        ]
                    )
                    options = {"description": description, "release": released}
                    lua_options = (
                        "{description="
                        + json.dumps(options["description"])
                        + ",release="
                        + str(released).lower()
                        + "}"
                    )
                    hyprland.evaluate(
                        f"hl.bind({json.dumps(sequence)}, hl.dsp.exec_cmd({json.dumps(command)}), {lua_options})"
                    )
                binds.extend(
                    [{"key": symbol, "modmask": mask, "description": description}]
                )
            self._owned[action] = (hotkey, sequence, symbol, mask)
            active.append(format_hotkey_display(hotkey))
        status = "; ".join(issues) if issues else "Desktop shortcuts active"
        if status != self._status or active != self._active_hotkeys:
            self._status = status
            self._active_hotkeys = active
            self.status_changed.emit(status)
            log.info("Omarchy: %s", status)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._sync()
            except Exception as exc:
                log.warning("Omarchy shortcut registration: %s", exc)
            self._wake.wait(3)
            self._wake.clear()
        try:
            self._sync()
        except Exception:
            log.exception("Could not release Omarchy shortcut bindings")

    def close(self):
        self._stop.set()
        self._state_timer.stop()
        self._write_state()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
        self.bus.unregisterObject(PATH)
        self.bus.unregisterService(SERVICE)
        self._active_hotkeys = []
        self._publish_status("Desktop shortcuts stopped")
