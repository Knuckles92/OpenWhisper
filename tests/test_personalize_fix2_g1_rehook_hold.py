"""A hook refresh in the middle of a push-and-hold never leaves the recording running.

The periodic refresh re-registers the hooks every five minutes, whatever the
user is doing, so a hold can span it.
"""

import ctypes
import threading
import time
import types
from unittest.mock import patch

import pytest

import services.settings  # noqa: F401  (bind the real settings before stubbed configs)
from services import _mouse_hook_win
from services.runtime.hotkeys import HotkeyRuntime
from services.settings import RecordingTriggerMode
from tests.test_personalize_s6_backends import (
    _FakeKernel32,
    _FakeMouseListener,
    _FakeUser32,
    _mouse_event,
    _pynput_manager,
    _windows_manager,
)
from tests.test_personalize_s6_runtime import _Controller, _drain, _wire
from tests.test_push_hold_recording import _key_event


@pytest.fixture
def runtime():
    controller = _Controller()
    controller._watchdog_interval_ms = 1000
    instance = HotkeyRuntime(controller)
    controller.runtime = instance
    yield instance
    instance.cleanup()


def _wired_windows_manager(runtime, **hotkeys):
    _module, manager = _windows_manager(**hotkeys)
    _wire(runtime, manager)
    runtime.controller.hotkey_manager = manager
    return manager


def _windows_events(**hotkeys):
    _module, manager = _windows_manager(**hotkeys)
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    events = []
    manager.set_callbacks(
        on_record_press=lambda at: events.append(("press", at)),
        on_record_release=lambda at: events.append(("release", at)),
        on_command_press=lambda at: events.append(("command_press", at)),
        on_command_release=lambda at: events.append(("command_release", at)),
    )
    return manager, events


@pytest.mark.parametrize("refresh", ["rehook", "update_hotkeys"])
def test_windows_side_button_hold_survives_a_rehook(refresh):
    manager, events = _windows_events(record_toggle="mouse4")
    manager._on_mouse_button("mouse4", True, frozenset(), 3.0)
    if refresh == "rehook":
        manager.rehook()
    else:
        manager.update_hotkeys({"cancel": "kp -"})
    manager._on_mouse_button("mouse4", False, frozenset(), 9.0)

    assert events == [("press", 3.0), ("release", 9.0)]
    assert manager._record_key_held is False


def test_windows_side_button_command_hold_survives_a_rehook():
    manager, events = _windows_events(command_mode="ctrl+mouse5")
    manager._on_mouse_button("mouse5", True, frozenset({"ctrl"}), 3.0)
    manager.rehook()
    manager._on_mouse_button("mouse5", False, frozenset(), 9.0)

    assert events == [("command_press", 3.0), ("command_release", 9.0)]


def test_capture_suspension_still_forgets_side_button_holds():
    manager, events = _windows_events(record_toggle="mouse4")
    manager._on_mouse_button("mouse4", True, frozenset(), 3.0)
    manager.set_capture_suspended(True)
    manager.set_capture_suspended(False)

    assert manager._record_key_held is False


def test_windows_side_button_hold_across_a_rehook_stops_the_recording(runtime):
    manager = _wired_windows_manager(runtime, record_toggle="mouse4")
    start = time.monotonic()
    manager._on_mouse_button("mouse4", True, frozenset(), start)
    _drain(runtime)
    runtime.rehook_keyboard()
    manager._on_mouse_button("mouse4", False, frozenset(), start + 2.0)
    _drain(runtime)

    assert runtime.controller.calls == ["start", "stop"]
    assert runtime.controller.recorder.is_recording is False


def test_keyboard_hold_across_a_rehook_stops_the_recording(runtime):
    manager = _wired_windows_manager(runtime)
    down, up = _key_event("down", "*"), _key_event("up", "*")
    assert manager._handle_keyboard_event(down) is False
    _drain(runtime)
    time.sleep(0.3)
    runtime.rehook_keyboard()
    # The key is still down, so Windows keeps repeating it.
    assert manager._handle_keyboard_event(down) is False
    assert manager._handle_keyboard_event(up) is False
    _drain(runtime)

    assert runtime.controller.calls == ["start", "stop"]


def test_lost_release_lets_the_next_press_and_release_stop_the_recording(runtime):
    start = time.monotonic()
    runtime._queue_record_press(start)
    # The release never arrives; the user presses and lets go again.
    runtime._queue_record_press(start + 5.0)
    runtime._queue_record_release(start + 5.2)
    _drain(runtime)

    assert runtime.controller.calls == ["start", "stop"]
    assert runtime._hold_state == "idle"


def test_press_during_another_recording_is_still_ignored(runtime):
    start = time.monotonic()
    runtime._queue_record_press(start)
    runtime._queue_record_release(start + 1.0)
    _drain(runtime)
    # A recording started elsewhere (tray, profile) owns the recorder.
    runtime.controller.recorder.is_recording = True
    runtime._queue_record_press(start + 2.0)
    runtime._queue_record_release(start + 3.0)
    _drain(runtime)

    assert runtime.controller.calls == ["start", "stop"]


def test_x11_side_button_hold_survives_a_rehook():
    module, manager = _pynput_manager()
    _FakeMouseListener.instances = []
    module._pynput_mouse_module = types.SimpleNamespace(Listener=_FakeMouseListener)
    keyboard_listener = types.SimpleNamespace(daemon=False, start=lambda: None, stop=lambda: None)
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    events = []
    manager.set_callbacks(
        on_record_press=lambda at: events.append("press"),
        on_record_release=lambda at: events.append("release"),
    )
    back = types.SimpleNamespace(name="button8")

    with patch.object(module, "mouse_shortcuts_supported", return_value=True), patch.object(
        module, "is_wayland_session", return_value=False
    ), patch.object(
        module, "get_listener_class", return_value=lambda **_kwargs: keyboard_listener
    ):
        manager._use_carbon = False
        manager._listener = keyboard_listener
        manager.update_hotkeys({"record_toggle": "mouse4"})
        _FakeMouseListener.instances[-1].on_click(0, 0, back, True)
        manager.rehook()
        _FakeMouseListener.instances[-1].on_click(0, 0, back, False)
        manager.cleanup()

    assert events == ["press", "release"]


class _User32(_FakeUser32):
    def SetWindowsHookExW(self, kind, proc, module, thread):
        self.quit = threading.Event()  # a fresh message loop per installation
        return super().SetWindowsHookExW(kind, proc, module, thread)


def test_real_mouse_hook_delivers_the_release_after_a_refresh(runtime, monkeypatch):
    monkeypatch.setattr("services.diagnostics.record_metrics", lambda **_values: None)
    manager = _wired_windows_manager(runtime)
    manager._mouse_hook = _mouse_hook_win.MouseButtonHook(
        manager._claims_mouse_button, manager._on_mouse_button,
        libraries=(_User32(), _FakeKernel32(), lambda function: function),
    )
    user32 = manager._mouse_hook._libraries[0]
    manager.update_hotkeys({"record_toggle": "mouse4"})
    infos = []

    def send(message):
        assert manager._mouse_hook._run.ready.wait(2)
        infos.append(_mouse_event(1))
        return user32.proc(0, message, ctypes.addressof(infos[-1]))

    try:
        assert send(_mouse_hook_win.WM_XBUTTONDOWN) == 1
        time.sleep(0.3)
        runtime.rehook_keyboard()
        assert send(_mouse_hook_win.WM_XBUTTONUP) == 1
        deadline = time.monotonic() + 2
        while runtime.controller.calls != ["start", "stop"] and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        manager.cleanup()

    assert runtime.controller.calls == ["start", "stop"]
