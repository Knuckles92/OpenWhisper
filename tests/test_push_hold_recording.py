"""Push-and-hold recording: backend dispatch and HotkeyRuntime handlers.

The Windows backend is loaded through a stubbed ``keyboard`` module (mirroring
tests/test_hotkey_manager.py) and the pynput backend through a stubbed
``pynput`` package, because pynput is not installed on Windows.
"""
import importlib.util
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from config import config

# Import the real settings module before any config stubbing below so the
# backends under test bind the production RecordingTriggerMode.
import services.settings  # noqa: F401
from services.settings import RecordingTriggerMode

ROOT = Path(__file__).resolve().parents[1]

SETTLE_SECONDS = 0.05


class _CallbackLog:
    """Records invocations from the backends' daemon dispatch threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = []
        self.arrived = threading.Event()

    def __call__(self, *args):
        with self._lock:
            self.calls.append(args)
        self.arrived.set()

    def wait(self, timeout=2.0):
        return self.arrived.wait(timeout)

    def count(self):
        with self._lock:
            return len(self.calls)


def _load_windows_backend(held_modifiers=None):
    held = held_modifiers if held_modifiers is not None else set()
    keyboard_stub = types.SimpleNamespace(
        KEY_DOWN="down",
        KEY_UP="up",
        hook=lambda *args, **kwargs: None,
        is_pressed=lambda name: name in held,
        unhook_all=lambda: None,
    )
    config_stub = types.SimpleNamespace(
        config=types.SimpleNamespace(
            DEFAULT_HOTKEYS={
                "record_toggle": "kp *",
                "cancel": "kp -",
                "enable_disable": "ctrl+alt+kp *",
                "minimize_tray": "ctrl+alt+m",
                "meeting_toggle": "",
            },
            HOTKEY_DEBOUNCE_MS=300,
        )
    )
    module_path = ROOT / "services" / "_hotkey_keyboard.py"
    spec = importlib.util.spec_from_file_location(
        "test_push_hold_keyboard_backend", module_path
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"keyboard": keyboard_stub, "config": config_stub}):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module


def _load_pynput_backend():
    class _Key:
        def __init__(self, name):
            self.name = name

    _Key.cmd = _Key("cmd")
    _Key.cmd_l = _Key("cmd_l")
    _Key.cmd_r = _Key("cmd_r")
    _Key.ctrl = _Key("ctrl")
    _Key.ctrl_l = _Key("ctrl_l")
    _Key.ctrl_r = _Key("ctrl_r")
    _Key.alt = _Key("alt")
    _Key.alt_l = _Key("alt_l")
    _Key.alt_r = _Key("alt_r")
    _Key.shift = _Key("shift")
    _Key.shift_l = _Key("shift_l")
    _Key.shift_r = _Key("shift_r")

    class _KeyCode:
        def __init__(self, char=None, vk=None):
            self.char = char
            self.vk = vk

    class _Listener:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    keyboard_ns = types.SimpleNamespace(
        Key=_Key, KeyCode=_KeyCode, Listener=_Listener, Controller=object
    )
    pynput_pkg = types.ModuleType("pynput")
    pynput_pkg.keyboard = keyboard_ns
    pynput_kb = types.ModuleType("pynput.keyboard")

    module_path = ROOT / "services" / "_hotkey_pynput.py"
    spec = importlib.util.spec_from_file_location(
        "test_push_hold_pynput_backend", module_path
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules, {"pynput": pynput_pkg, "pynput.keyboard": pynput_kb}
    ):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    module._pynput_keyboard_module = keyboard_ns
    return module


def _key_event(event_type, name, keypad=True):
    return types.SimpleNamespace(event_type=event_type, name=name, is_keypad=keypad)


class TestWindowsBackendPushHold(unittest.TestCase):
    def setUp(self):
        self.module = _load_windows_backend()

    def _manager(self):
        return self.module.HotkeyManager()

    def test_hold_fires_press_once_and_release_on_key_up(self):
        manager = self._manager()
        manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
        presses, releases = _CallbackLog(), _CallbackLog()
        manager.on_record_press = presses
        manager.on_record_release = releases

        self.assertIs(manager._handle_keyboard_event(_key_event("down", "*")), False)
        self.assertTrue(presses.wait())

        # Auto-repeat KEY_DOWNs while held must not re-fire the press.
        manager._handle_keyboard_event(_key_event("down", "*"))
        self.assertIs(manager._handle_keyboard_event(_key_event("up", "*")), False)
        self.assertTrue(releases.wait())

        time.sleep(SETTLE_SECONDS)
        self.assertEqual(presses.count(), 1)
        self.assertEqual(releases.count(), 1)
        self.assertFalse(manager._record_key_held)

    def test_release_matches_main_key_even_without_modifiers(self):
        held = set()
        self.module = _load_windows_backend(held_modifiers=held)
        manager = self._manager()
        manager.hotkeys["record_toggle"] = "ctrl+alt+r"
        manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
        presses, releases = _CallbackLog(), _CallbackLog()
        manager.on_record_press = presses
        manager.on_record_release = releases

        held.update(("ctrl", "alt"))
        self.assertIs(manager._handle_keyboard_event(_key_event("down", "r", keypad=False)), False)
        self.assertTrue(presses.wait())

        # Modifiers released before the main key; the release must still match.
        held.clear()
        self.assertIs(manager._handle_keyboard_event(_key_event("up", "r", keypad=False)), False)
        self.assertTrue(releases.wait())

    def test_toggle_mode_suppresses_release_without_dispatching(self):
        manager = self._manager()
        toggles, releases = _CallbackLog(), _CallbackLog()
        manager.on_record_toggle = toggles
        manager.on_record_release = releases

        self.assertIs(manager._handle_keyboard_event(_key_event("down", "*")), False)
        self.assertTrue(toggles.wait())
        self.assertIs(manager._handle_keyboard_event(_key_event("up", "*")), False)

        time.sleep(SETTLE_SECONDS)
        self.assertEqual(releases.count(), 0)

    def test_release_without_press_passes_through(self):
        manager = self._manager()
        releases = _CallbackLog()
        manager.on_record_release = releases

        self.assertIs(manager._handle_keyboard_event(_key_event("up", "*")), True)
        time.sleep(SETTLE_SECONDS)
        self.assertEqual(releases.count(), 0)


class TestPynputBackendPushHold(unittest.TestCase):
    HOTKEYS = {
        "record_toggle": "ctrl+alt+r",
        "cancel": "ctrl+alt+escape",
        "enable_disable": "ctrl+alt+shift+r",
        "minimize_tray": "ctrl+alt+m",
        "meeting_toggle": "",
    }

    def setUp(self):
        self.module = _load_pynput_backend()

    def _manager(self):
        with patch.object(self.module.HotkeyManager, "_setup_keyboard_hook"):
            return self.module.HotkeyManager(dict(self.HOTKEYS))

    def _press_record_combo(self, manager):
        manager._on_press(self.module.pynput_keyboard.Key.ctrl)
        manager._on_press(self.module.pynput_keyboard.Key.alt)
        manager._on_press(self.module.pynput_keyboard.KeyCode(char="r"))

    def test_available_backend_starts_global_listener(self):
        manager = self.module.HotkeyManager(dict(self.HOTKEYS))

        self.assertTrue(manager.backend_available)
        self.assertEqual(manager.backend_name, "pynput")
        self.assertIsNotNone(manager._listener)
        manager.cleanup()

    def test_hold_fires_press_and_global_release(self):
        manager = self._manager()
        manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
        presses, releases = _CallbackLog(), _CallbackLog()
        manager.on_record_press = presses
        manager.on_record_release = releases

        self._press_record_combo(manager)
        self.assertTrue(presses.wait())

        # Auto-repeat press is filtered by the pressed-main-key set.
        manager._on_press(self.module.pynput_keyboard.KeyCode(char="r"))
        # Modifiers released before the main key; release must still match.
        manager._on_release(self.module.pynput_keyboard.Key.ctrl)
        manager._on_release(self.module.pynput_keyboard.Key.alt)
        manager._on_release(self.module.pynput_keyboard.KeyCode(char="r"))
        self.assertTrue(releases.wait())

        time.sleep(SETTLE_SECONDS)
        self.assertEqual(presses.count(), 1)
        self.assertEqual(releases.count(), 1)
        self.assertFalse(manager._record_key_held)

    def test_qt_release_entry_dispatches_and_dedupes(self):
        manager = self._manager()
        manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
        releases = _CallbackLog()
        manager.on_record_release = releases

        manager._record_key_held = True
        self.assertTrue(
            manager.handle_hotkey_release(frozenset(), "r", source="qt")
        )
        self.assertTrue(releases.wait())

        # Flag cleared: a repeat release no longer matches.
        self.assertFalse(
            manager.handle_hotkey_release(frozenset(), "r", source="qt")
        )

        # Flag re-set by a second hold, but within the 0.2s cross-source
        # dedupe window the duplicate release is swallowed.
        manager._record_key_held = True
        self.assertTrue(
            manager.handle_hotkey_release(frozenset(), "r", source="qt")
        )
        time.sleep(SETTLE_SECONDS)
        self.assertEqual(releases.count(), 1)

    def test_carbon_released_event_routes_to_record_release(self):
        manager = self._manager()
        manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
        cancels, releases = _CallbackLog(), _CallbackLog()
        manager.on_cancel = cancels
        manager.on_record_release = releases

        manager.trigger_action("record_toggle", released=True)
        self.assertTrue(releases.wait())

        # Released events for non-record actions are ignored.
        manager.trigger_action("cancel", released=True)
        time.sleep(SETTLE_SECONDS)
        self.assertEqual(cancels.count(), 0)

    def test_carbon_press_routes_to_record_press_in_push_hold(self):
        # Carbon presses arrive via trigger_action directly, with no
        # handle_hotkey_press per-key matching on macOS.
        manager = self._manager()
        manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
        toggles, presses = _CallbackLog(), _CallbackLog()
        manager.on_record_toggle = toggles
        manager.on_record_press = presses

        manager.trigger_action("record_toggle")
        self.assertTrue(presses.wait())

        time.sleep(SETTLE_SECONDS)
        self.assertEqual(toggles.count(), 0)

    def test_toggle_mode_press_still_fires_toggle(self):
        manager = self._manager()
        toggles = _CallbackLog()
        manager.on_record_toggle = toggles

        self._press_record_combo(manager)
        self.assertTrue(toggles.wait())


class _FakeRecorder:
    def __init__(self, recording=False, has_data=True):
        self.is_recording = recording
        self._has_data = has_data

    def has_recording_data(self):
        return self._has_data


class _FakeSignal:
    def __init__(self):
        self.emitted = []

    def emit(self, value):
        self.emitted.append(value)


class _FakeController:
    def __init__(self, recorder, start_accepted=True):
        self.recorder = recorder
        self.hotkey_manager = None
        self.status_update = _FakeSignal()
        self.calls = []
        self._start_accepted = start_accepted

    def start_recording(self):
        self.calls.append("start")
        self.recorder.is_recording = self._start_accepted
        return self._start_accepted

    def stop_recording(self):
        self.calls.append("stop")

    def cancel(self):
        self.calls.append("cancel")


class TestPushHoldRuntimeHandlers(unittest.TestCase):
    def setUp(self):
        from services.runtime.hotkeys import HotkeyRuntime

        self.HotkeyRuntime = HotkeyRuntime

    def test_long_hold_stops(self):
        controller = _FakeController(_FakeRecorder())
        runtime = self.HotkeyRuntime(controller)

        with patch.object(time, "monotonic", side_effect=[100.0, 100.5, 100.5]):
            runtime.record_key_pressed()
            runtime.record_key_released()

        self.assertEqual(controller.calls, ["start", "stop"])

    def test_short_tap_cancels(self):
        controller = _FakeController(_FakeRecorder())
        runtime = self.HotkeyRuntime(controller)

        with patch.object(time, "monotonic", side_effect=[100.0, 100.1, 100.1]):
            runtime.record_key_pressed()
            runtime.record_key_released()

        self.assertEqual(controller.calls, ["start", "cancel"])

    def test_refused_start_makes_release_a_no_op(self):
        controller = _FakeController(_FakeRecorder(), start_accepted=False)
        runtime = self.HotkeyRuntime(controller)

        runtime.record_key_pressed()
        runtime.record_key_released()

        self.assertEqual(controller.calls, ["start"])

    def test_press_during_post_roll_is_ignored(self):
        controller = _FakeController(_FakeRecorder(recording=True))
        runtime = self.HotkeyRuntime(controller)

        runtime.record_key_pressed()

        self.assertEqual(controller.calls, [])

    def test_release_before_stream_open_waits_then_no_ops(self):
        # Model a start that never opens the stream: is_recording stays False
        # and the release must give up quietly instead of stopping/canceling.
        controller = _FakeController(_FakeRecorder(), start_accepted=True)

        def start_without_stream():
            controller.calls.append("start")
            return True

        controller.start_recording = start_without_stream
        runtime = self.HotkeyRuntime(controller)

        clock = {"now": 100.0}

        def fake_monotonic():
            clock["now"] += 0.05
            return clock["now"]

        with patch.object(time, "monotonic", side_effect=fake_monotonic), patch.object(
            time, "sleep", lambda _seconds: None
        ):
            runtime.record_key_pressed()
            runtime.record_key_released()

        self.assertEqual(controller.calls, ["start"])

    def test_release_after_cancel_hotkey_is_a_no_op(self):
        # Cancel clears the captured frames while post-roll keeps
        # is_recording True; the release must not run the stop path.
        recorder = _FakeRecorder(has_data=False)
        controller = _FakeController(recorder)
        runtime = self.HotkeyRuntime(controller)

        with patch.object(time, "monotonic", side_effect=[100.0, 101.0, 101.0]):
            runtime.record_key_pressed()
            runtime.record_key_released()

        self.assertEqual(controller.calls, ["start"])

    def test_set_mode_applies_to_manager_and_resets_state(self):
        controller = _FakeController(_FakeRecorder())
        applied = []
        controller.hotkey_manager = types.SimpleNamespace(
            set_record_mode=applied.append
        )
        runtime = self.HotkeyRuntime(controller)
        runtime._record_press_monotonic = 42.0
        runtime._record_start_accepted = True

        runtime.set_recording_trigger_mode(RecordingTriggerMode.PUSH_HOLD)
        self.assertEqual(applied, [RecordingTriggerMode.PUSH_HOLD])
        self.assertIsNone(runtime._record_press_monotonic)
        self.assertFalse(runtime._record_start_accepted)
        self.assertEqual(
            controller.status_update.emitted, ["Push-and-hold recording enabled"]
        )

        runtime.set_recording_trigger_mode("bogus")
        self.assertEqual(applied[-1], config.RECORDING_TRIGGER_MODE)


if __name__ == "__main__":
    unittest.main()
