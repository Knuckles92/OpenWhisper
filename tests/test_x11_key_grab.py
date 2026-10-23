"""X11 shortcuts are grabbed so they don't also type into the focused app.

The grab itself needs a real X server: the end-to-end test starts Xvfb and
presses keys with xdotool, and is skipped where either is missing.
"""
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

from services import _hotkey_pynput
from services._x11_key_grab import keysym_for


def test_keypad_and_raw_keysyms_map_without_a_display():
    assert keysym_for("kp *") == 0xFFAA
    assert keysym_for("kp -") == 0xFFAD
    assert keysym_for("vk65481") == 65481
    assert keysym_for("") is None
    assert keysym_for("kp nonsense") is None


class _FakeGrabber:
    def __init__(self):
        self.calls = []

    def register_hotkeys(self, hotkeys):
        self.calls.append(("register", dict(hotkeys)))

    def unregister_all(self):
        self.calls.append(("unregister",))

    def close(self):
        self.calls.append(("close",))


def _manager_with(grabber):
    manager = _hotkey_pynput.HotkeyManager.__new__(_hotkey_pynput.HotkeyManager)
    manager.hotkeys = {"record_toggle": "kp *", "cancel": "kp -"}
    manager._dynamic_hotkeys = {}
    manager._dynamic_held = {"profile": {}, "transform": {}}
    manager._carbon_registrar = None
    manager._x11_grabber = grabber
    manager._mouse_listener = None
    manager._listener = None
    manager._pressed_modifiers = set()
    manager._pressed_main_keys = set()
    manager._record_key_held = False
    manager._command_key_held = False
    manager.capture_suspended = False
    manager._sync_mouse_listener = lambda: None
    return manager


def test_shortcut_changes_and_capture_update_the_grabs():
    grabber = _FakeGrabber()
    manager = _manager_with(grabber)

    manager.update_hotkeys({"record_toggle": "f8"})
    manager.set_capture_suspended(True)
    manager.update_hotkeys({"cancel": "f9"})
    manager.set_capture_suspended(False)
    manager.cleanup()

    assert grabber.calls == [
        ("register", {"record_toggle": "f8", "cancel": "kp -"}),
        # While a shortcut field captures, keys must reach it.
        ("unregister",),
        ("register", {"record_toggle": "f8", "cancel": "f9"}),
        ("close",),
    ]
    assert manager._x11_grabber is None


def _free_display() -> int:
    for number in range(91, 120):
        if not os.path.exists(f"/tmp/.X11-unix/X{number}") and not os.path.exists(f"/tmp/.X{number}-lock"):
            return number
    pytest.skip("no free X display number")


@pytest.fixture
def xvfb(monkeypatch):
    if not sys.platform.startswith("linux") or not shutil.which("Xvfb") or not shutil.which("xdotool"):
        pytest.skip("needs Xvfb and xdotool")
    number = _free_display()
    server = subprocess.Popen(["Xvfb", f":{number}", "-screen", "0", "640x480x24"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    name = f":{number}"
    deadline = time.monotonic() + 10
    while not os.path.exists(f"/tmp/.X11-unix/X{number}"):
        if time.monotonic() > deadline or server.poll() is not None:
            server.kill()
            pytest.skip("Xvfb did not start")
        time.sleep(0.05)
    monkeypatch.setenv("DISPLAY", name)
    yield name
    server.terminate()
    server.wait(5)


def test_a_grabbed_shortcut_never_reaches_the_focused_window(xvfb):
    import Xlib.threaded  # noqa: F401
    from Xlib import X
    from Xlib import display as xdisplay

    from services._x11_key_grab import X11KeyGrabber

    app = xdisplay.Display(xvfb)
    screen = app.screen()
    window = screen.root.create_window(0, 0, 100, 100, 0, screen.root_depth,
                                       event_mask=X.KeyPressMask)
    window.map()
    app.sync()
    window.set_input_focus(X.RevertToParent, X.CurrentTime)
    app.sync()
    typed = []

    def read():
        while True:
            try:
                event = app.next_event()
            except Exception:
                return
            if event.type == X.KeyPress:
                typed.append(app.keycode_to_keysym(event.detail, 0))

    threading.Thread(target=read, daemon=True).start()

    def press(name):
        subprocess.run(["xdotool", "key", name], check=True,
                       env={**os.environ, "DISPLAY": xvfb})

    def settle():
        time.sleep(0.3)

    grabber = X11KeyGrabber(xdisplay.Display(xvfb))
    try:
        grabber.register_hotkeys({"record_toggle": "kp *", "cancel": "ctrl+alt+m"})
        press("KP_Multiply")
        press("a")
        settle()
        assert typed == [ord("a")]

        grabber.unregister_all()
        typed.clear()
        press("KP_Multiply")
        settle()
        assert typed == [0xFFAA]
    finally:
        grabber.close()
        app.close()
