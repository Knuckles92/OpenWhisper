"""X11 shortcuts are grabbed so they don't also type into the focused app.

These tests need a real X server: each starts its own Xvfb and is skipped
where there is none. The end-to-end grab test also presses keys with xdotool.

The hook lifecycle tests are #141. Replacing a hook has to end the old one's
threads and give its connections back even while nobody types, and a hook the
server has no room for has to say so instead of freezing the next refresh.
"""
import contextlib
import os
import select
import shutil
import subprocess
import sys
import threading
import time

import pytest

from services import _hotkey_pynput
from services._x11_key_grab import keysym_for

#: The fewest clients an X server can be limited to, so a test can fill it.
MAX_CLIENTS = 64


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


@pytest.fixture
def x_server(monkeypatch):
    """The display name of a private X server with room for few clients."""
    if not sys.platform.startswith("linux") or not shutil.which("Xvfb"):
        pytest.skip("needs Xvfb")
    # The server picks a free display itself and writes the number once it
    # listens, so tests running side by side never end up sharing one.
    # -noreset: it would otherwise start over when its last client leaves,
    # refusing whoever connects next.
    read_fd, write_fd = os.pipe()
    server = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "640x480x24",
         "-noreset", "-maxclients", str(MAX_CLIENTS)],
        pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as started:
            ready, _, _ = select.select([started], [], [], 10)
            number = started.readline().strip() if ready else ""
        if not number:
            pytest.skip("Xvfb did not start")
        monkeypatch.setenv("DISPLAY", f":{number}")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        yield f":{number}"
    finally:
        server.terminate()
        server.wait(5)


@pytest.fixture
def xvfb(x_server):
    if not shutil.which("xdotool"):
        pytest.skip("needs xdotool")
    return x_server


@pytest.fixture
def x_clients(x_server):
    """Counts the clients connected to the private X server."""
    from Xlib import display as xdisplay

    monitor = xdisplay.Display(x_server)
    yield lambda: len(monitor.res_query_clients().clients)
    monitor.close()


def _within(seconds: float, condition) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def _returns(call, seconds: float = 5.0) -> bool:
    """Whether ``call`` comes back; a hang fails the test, not the whole run."""
    done = threading.Event()
    threading.Thread(target=lambda: (call(), done.set()), daemon=True).start()
    return done.wait(seconds)


def _asleep_on(display) -> bool:
    """Wait until a thread sleeps reading ``display``, with nothing to read.

    That is where stopping used to go unheard, so stop from there.
    """
    if not _within(5, lambda: display.display.recv_active):
        return False
    time.sleep(0.05)
    return True


def _recording(listener) -> bool:
    listener.wait()
    return _asleep_on(listener._display_record)


@contextlib.contextmanager
def _someone_typing(display_name):
    from Xlib import X
    from Xlib import display as xdisplay
    from Xlib.ext import xtest

    keyboard = xdisplay.Display(display_name)
    keycode = keyboard.keysym_to_keycode(ord("a"))
    done = threading.Event()

    def type_away():
        while not done.is_set():
            xtest.fake_input(keyboard, X.KeyPress, keycode)
            xtest.fake_input(keyboard, X.KeyRelease, keycode)
            keyboard.sync()
            time.sleep(0.002)

    typist = threading.Thread(target=type_away, daemon=True)
    typist.start()
    try:
        yield
    finally:
        done.set()
        typist.join(5)
        keyboard.close()


def _take_every_free_slot(display_name) -> list:
    """Connect until the server refuses, which is where the leak ended up."""
    from Xlib import display as xdisplay
    from Xlib.error import DisplayConnectionError

    others = []
    with pytest.raises(DisplayConnectionError, match="Maximum number of clients"):
        while True:
            others.append(xdisplay.Display(display_name))
    return others


def test_closing_the_grabber_ends_its_thread_and_gives_back_its_connection(x_server, x_clients):
    from Xlib import display as xdisplay

    from services._x11_key_grab import X11KeyGrabber

    clients = x_clients()
    # A close that strands the drain does so about every other time.
    for _ in range(8):
        grabber = X11KeyGrabber(xdisplay.Display(x_server))
        grabber.register_hotkeys({"record_toggle": "kp *"})
        assert _asleep_on(grabber._display)

        grabber.close()

        assert not grabber._thread.is_alive()
    assert _within(5, lambda: x_clients() == clients)


@pytest.mark.parametrize("typing", [False, True], ids=["nobody types", "someone types"])
def test_a_hook_refresh_leaves_nothing_of_the_old_hook_behind(x_server, x_clients, typing):
    manager = _hotkey_pynput.HotkeyManager({"record_toggle": "kp *", "command_mode": "mouse4"})
    try:
        with _someone_typing(x_server) if typing else contextlib.nullcontext():
            assert manager.backend_name == "pynput"
            clients = x_clients()
            for _ in range(5):
                keyboard, mouse, grabber = (
                    manager._listener, manager._mouse_listener, manager._x11_grabber
                )
                assert _recording(keyboard) and _recording(mouse)
                assert _asleep_on(grabber._display)

                manager.rehook()

                assert not keyboard.is_alive() and not mouse.is_alive()
                assert not grabber._thread.is_alive()
            assert manager.backend_available
            assert _within(5, lambda: x_clients() == clients)
    finally:
        assert _returns(manager.cleanup)


def test_a_hook_the_server_has_no_room_for_says_so_and_recovers(x_server, x_clients):
    manager = _hotkey_pynput.HotkeyManager({"record_toggle": "kp *"})
    others = []
    try:
        assert manager.backend_available
        manager.cleanup()
        others = _take_every_free_slot(x_server)
        full = x_clients()

        # The two refreshes that ended #141's log: the first reported success
        # with its listener thread already dead, the second never returned.
        for _ in range(2):
            assert _returns(manager.rehook)
            assert not manager.backend_available
            assert "Maximum number of clients" in manager.backend_error
            assert manager._listener is None

        # With one slot the listener's first connection opens and its second
        # is refused; the first has to be given back.
        others.pop().close()
        assert _within(5, lambda: x_clients() == full - 1)
        assert _returns(manager.rehook)
        assert not manager.backend_available
        assert _within(5, lambda: x_clients() == full - 1)

        while others:
            others.pop().close()
        assert _returns(manager.rehook)
        assert manager.backend_available and manager._x11_grabber is not None
        assert _recording(manager._listener)
    finally:
        for other in others:
            other.close()
        assert _returns(manager.cleanup)


def test_a_hook_whose_connection_died_is_logged_and_replaced_by_the_next_refresh(
    x_server, x_clients, caplog
):
    import socket

    manager = _hotkey_pynput.HotkeyManager({"record_toggle": "kp *"})
    try:
        clients = x_clients()
        listener = manager._listener
        assert _recording(listener)

        listener._display_record.display.socket.shutdown(socket.SHUT_RDWR)

        assert _returns(lambda: threading.Thread.join(listener))
        assert "lost its connection" in caplog.text
        assert _returns(manager.rehook)
        assert manager.backend_available and _recording(manager._listener)
        assert _within(5, lambda: x_clients() == clients)
    finally:
        assert _returns(manager.cleanup)


def test_a_shortcut_field_listener_ends_with_a_key_or_when_dismissed(x_server, x_clients):
    # The shortcut field starts a listener and joins it; a key ends it from
    # its own callback, and dismissing the field stops it from the Qt thread.
    clients = x_clients()

    def capture():
        listener = _hotkey_pynput.get_listener_class()(on_press=lambda _key: False)
        listener.start()
        assert _recording(listener)
        return listener

    listener = capture()
    with _someone_typing(x_server):
        assert _returns(listener.join)
    assert _within(5, lambda: x_clients() == clients)

    listener = capture()
    listener.stop()
    assert _returns(listener.join)
    assert _within(5, lambda: x_clients() == clients)


def test_a_shortcut_field_listener_the_server_refuses_fails_instead_of_hanging(x_server):
    from Xlib.error import DisplayConnectionError

    # pynput is loaded long before a server fills up; importing it connects.
    listener_class = _hotkey_pynput.get_listener_class()
    others = _take_every_free_slot(x_server)
    try:
        listener = listener_class(on_press=lambda _key: False)
        listener.start()
        with pytest.raises(DisplayConnectionError):
            listener.join(5)
        assert _returns(listener.stop)
    finally:
        for other in others:
            other.close()


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
