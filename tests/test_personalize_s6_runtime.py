"""Ordered hotkey dispatch, the hands-free latch and shortcut families."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import config
from services._hotkey_common import OrderedDispatcher
from services.runtime import hotkeys as runtime_module
from services.runtime.hotkeys import HotkeyRuntime, is_hotkey_window
from services.settings import RecordingTriggerMode, SettingsKey
from services.text_transforms import Transform
from tests.test_push_hold_recording import (
    _key_event,
    _load_pynput_backend,
    _load_windows_backend,
)

WINDOW = config.RECORD_LATCH_WINDOW_MS / 1000


def _wait_until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args[0] if len(args) == 1 else args)


class _Recorder:
    def __init__(self):
        self.is_recording = False

    def has_recording_data(self):
        return self.is_recording


class _Controller:
    """Records what the runtime asks for; a recording ending reports back."""

    def __init__(self):
        self.recorder = _Recorder()
        self.hotkey_manager = SimpleNamespace(hotkeys={"record_toggle": "f9"})
        self.status_update = _Signal()
        self.hands_free_changed = _Signal()
        self.transform_requested = _Signal()
        self.profile_record_requested = _Signal()
        self.calls = []
        self.command_threads = []
        self.runtime = None

    def start_recording(self):
        self.calls.append("start")
        self.recorder.is_recording = True
        return True

    def stop_recording(self):
        self.calls.append("stop")
        self._ended()

    def cancel(self):
        self.calls.append("cancel")
        self._ended()

    def _ended(self):
        if self.recorder.is_recording:
            self.recorder.is_recording = False
            self.runtime._on_recording_state_changed(False)

    def command_key_pressed(self, at):
        self.calls.append(("command_press", at))
        self.command_threads.append(threading.current_thread().name)

    def command_key_released(self, at):
        self.calls.append(("command_release", at))
        self.command_threads.append(threading.current_thread().name)


@pytest.fixture
def latch_on(monkeypatch):
    monkeypatch.setattr(runtime_module, "resolve_recording_hands_free_latch", lambda: True)


@pytest.fixture
def runtime():
    controller = _Controller()
    instance = HotkeyRuntime(controller)
    controller.runtime = instance
    yield instance
    instance.cleanup()


def _drain(runtime):
    done = threading.Event()
    runtime._dispatcher.submit(done.set)
    assert done.wait(2)


def _tap_then_press(runtime, gap):
    """Tap the record key (100 ms), then press it again ``gap`` seconds later."""
    start = time.monotonic()
    runtime._queue_record_press(start)
    runtime._queue_record_release(start + 0.1)
    runtime._queue_record_press(start + 0.1 + gap)
    _drain(runtime)
    return start + 0.1 + gap


# The ordered dispatcher


def test_dispatcher_runs_calls_in_order_on_one_thread():
    dispatcher = OrderedDispatcher("test-dispatch")
    seen = []
    try:
        for index in range(50):
            dispatcher.submit(lambda index=index: seen.append((index, threading.current_thread().name)))
        done = threading.Event()
        dispatcher.submit(done.set)
        assert done.wait(2)
    finally:
        dispatcher.stop()
    assert [index for index, _ in seen] == list(range(50))
    assert {name for _, name in seen} == {"test-dispatch"}


def test_dispatcher_deadline_waits_for_queued_calls_and_can_be_replaced():
    dispatcher = OrderedDispatcher("test-deadline")
    seen = []
    try:
        # Hold the worker first, so no deadline can fire between the calls
        # that replace or cancel it, however late this thread is scheduled.
        gate = threading.Event()
        dispatcher.submit(gate.wait, 5)
        dispatcher.call_at(time.monotonic(), seen.append, "first deadline")
        dispatcher.call_at(time.monotonic() + 0.05, seen.append, "deadline")
        dispatcher.submit(seen.append, "queued")
        time.sleep(0.1)
        gate.set()
        assert _wait_until(lambda: len(seen) == 2)
        gate.clear()
        dispatcher.submit(gate.wait, 5)
        dispatcher.call_at(time.monotonic(), seen.append, "canceled")
        dispatcher.cancel_deadline()
        gate.set()
        time.sleep(0.15)
    finally:
        dispatcher.stop()
    assert seen == ["queued", "deadline"]


def test_dispatcher_survives_a_failing_call_and_restarts_after_stop():
    dispatcher = OrderedDispatcher("test-restart")
    seen = []
    try:
        dispatcher.submit(lambda: 1 / 0)
        dispatcher.submit(seen.append, "after failure")
        assert _wait_until(lambda: seen == ["after failure"])
        dispatcher.stop()
        dispatcher.submit(seen.append, "after restart")
        assert _wait_until(lambda: seen[-1] == "after restart")
    finally:
        dispatcher.stop()


def test_a_slow_start_never_lets_its_release_run_first(runtime):
    controller = runtime.controller
    original_start = controller.start_recording

    def slow_start():
        time.sleep(0.2)
        return original_start()

    controller.start_recording = slow_start
    start = time.monotonic()
    runtime._queue_record_press(start)
    runtime._queue_record_release(start + 0.6)
    _drain(runtime)
    assert controller.calls == ["start", "stop"]


# The hands-free latch


@pytest.mark.parametrize("gap", [0.15, 0.35])
def test_a_second_press_soon_after_a_tap_keeps_recording(runtime, latch_on, gap):
    controller = runtime.controller
    pressed_at = _tap_then_press(runtime, gap)
    runtime._queue_record_release(pressed_at + 0.08)  # the latching press's release
    _drain(runtime)

    assert controller.calls == ["start"]
    assert controller.recorder.is_recording
    assert controller.hands_free_changed.emitted == [True]
    assert controller.status_update.emitted[-1] == "Hands-free · press F9 to stop"
    time.sleep(WINDOW + 0.1)
    assert controller.calls == ["start"]

    runtime._queue_record_press(pressed_at + 5)
    runtime._queue_record_release(pressed_at + 5.1)
    _drain(runtime)
    assert controller.calls == ["start", "stop"]
    assert controller.hands_free_changed.emitted == [True, False]


def test_a_press_after_the_window_cancels_the_tap_and_starts_over(runtime, latch_on):
    controller = runtime.controller
    _tap_then_press(runtime, 0.45)
    assert controller.calls == ["start", "cancel", "start"]
    assert controller.hands_free_changed.emitted == []


def test_a_lone_tap_cancels_only_once_the_window_closes(runtime, latch_on):
    controller = runtime.controller
    start = time.monotonic()
    runtime._queue_record_press(start)
    runtime._queue_record_release(start + 0.05)
    _drain(runtime)
    assert controller.calls == ["start"]
    assert _wait_until(lambda: controller.calls == ["start", "cancel"])
    assert time.monotonic() - start >= 0.05 + WINDOW - 0.02
    assert controller.hands_free_changed.emitted == []


def test_without_the_latch_a_tap_cancels_at_once(runtime, monkeypatch):
    monkeypatch.setattr(runtime_module, "resolve_recording_hands_free_latch", lambda: False)
    controller = runtime.controller
    _tap_then_press(runtime, 0.15)
    assert controller.calls == ["start", "cancel", "start"]


def test_a_long_hold_still_stops_with_the_latch_on(runtime, latch_on):
    controller = runtime.controller
    start = time.monotonic()
    runtime._queue_record_press(start)
    runtime._queue_record_release(start + 0.4)
    _drain(runtime)
    assert controller.calls == ["start", "stop"]


def test_a_recording_that_ends_elsewhere_leaves_hands_free(runtime, latch_on):
    controller = runtime.controller
    pressed_at = _tap_then_press(runtime, 0.15)
    controller.stop_recording()  # e.g. the tray
    _drain(runtime)
    assert controller.hands_free_changed.emitted == [True, False]

    runtime._queue_record_press(pressed_at + 3)
    _drain(runtime)
    assert controller.calls == ["start", "stop", "start"]


def test_a_tap_canceled_by_the_cancel_key_is_not_canceled_again(runtime, latch_on):
    controller = runtime.controller
    start = time.monotonic()
    runtime._queue_record_press(start)
    runtime._queue_record_release(start + 0.05)
    _drain(runtime)
    controller.cancel()
    time.sleep(WINDOW + 0.1)
    _drain(runtime)
    assert controller.calls == ["start", "cancel"]


def test_changing_the_trigger_mode_drops_the_latch(runtime, latch_on):
    controller = runtime.controller
    controller.hotkey_manager.set_record_mode = lambda _mode: None
    _tap_then_press(runtime, 0.15)
    runtime.set_recording_trigger_mode(RecordingTriggerMode.TOGGLE)
    assert controller.hands_free_changed.emitted == [True, False]
    assert controller.recorder.is_recording


def _wire(runtime, manager):
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    manager.set_callbacks(
        on_record_press=runtime._queue_record_press,
        on_record_release=runtime._queue_record_release,
        on_command_press=runtime._queue_command_press,
        on_command_release=runtime._queue_command_release,
    )


def test_windows_key_events_latch_and_stop(runtime, latch_on):
    module = _load_windows_backend()
    manager = module.HotkeyManager()
    _wire(runtime, manager)
    down, up = _key_event("down", "*"), _key_event("up", "*")
    for event in (down, up, down, up):
        assert manager._handle_keyboard_event(event) is False
    _drain(runtime)
    assert runtime.controller.hands_free_changed.emitted == [True]
    manager._handle_keyboard_event(down)
    manager._handle_keyboard_event(up)
    _drain(runtime)
    assert runtime.controller.calls == ["start", "stop"]


def test_pynput_listener_and_carbon_events_latch_and_stop(runtime, latch_on):
    module = _load_pynput_backend()
    with patch.object(module.HotkeyManager, "_setup_keyboard_hook"):
        manager = module.HotkeyManager({"record_toggle": "ctrl+alt+r"})
    _wire(runtime, manager)
    key = module.pynput_keyboard
    manager._on_press(key.Key.ctrl)
    manager._on_press(key.Key.alt)
    for _ in range(2):
        manager._on_press(key.KeyCode(char="r"))
        manager._on_release(key.KeyCode(char="r"))
    _drain(runtime)
    assert runtime.controller.hands_free_changed.emitted == [True]
    # Carbon and Hyprland deliver the action with a released flag instead.
    manager.trigger_action("record_toggle")
    manager.trigger_action("record_toggle", released=True)
    _drain(runtime)
    assert runtime.controller.calls == ["start", "stop"]


# Command Mode and shortcut families


def test_command_keys_reach_the_controller_on_the_dispatcher_thread(runtime):
    runtime._queue_command_press(10.0)
    runtime._queue_command_release(10.4)
    _drain(runtime)
    assert runtime.controller.calls == [("command_press", 10.0), ("command_release", 10.4)]
    assert set(runtime.controller.command_threads) == {"hotkey-dispatch"}


class _FamilyManager:
    def __init__(self, hotkeys):
        self.hotkeys = hotkeys
        self.families = {}

    def set_dynamic_hotkeys(self, namespace, hotkeys, callback):
        self.families[namespace] = (hotkeys, callback)


def test_transform_shortcuts_register_beside_profiles_and_skip_conflicts(runtime, monkeypatch):
    controller = runtime.controller
    controller.hotkey_manager = _FamilyManager({"record_toggle": "ctrl+alt+r", "cancel": "esc"})
    settings = {
        SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [
            {"id": "ticket", "name": "Ticket", "instructions": "Ticket.", "hotkey": "ctrl+alt+t"},
        ],
    }
    monkeypatch.setattr(runtime_module.settings_manager, "load_all_settings", lambda: settings)
    monkeypatch.setattr(
        runtime_module.text_transforms,
        "load_transforms",
        lambda _settings: [
            Transform("polish", "Polish", "Polish it.", "ctrl+alt+p"),
            Transform("clash", "Clash", "Clash.", "alt+ctrl+R"),
            Transform("plain", "Plain", "No key."),
        ],
    )
    runtime.refresh_profile_hotkeys()

    families = controller.hotkey_manager.families
    assert families["profile"][0] == {"ticket": "ctrl+alt+t"}
    assert families["transform"][0] == {"polish": "ctrl+alt+p"}
    families["transform"][1]("polish")
    assert controller.transform_requested.emitted == ["polish"]


def test_a_broken_transform_library_keeps_profile_shortcuts(runtime, monkeypatch):
    controller = runtime.controller
    controller.hotkey_manager = _FamilyManager({})
    monkeypatch.setattr(runtime_module.settings_manager, "load_all_settings", lambda: {})
    monkeypatch.setattr(
        runtime_module.text_transforms, "load_transforms", lambda _settings: 1 / 0
    )
    runtime.refresh_profile_hotkeys()
    assert controller.hotkey_manager.families["transform"][0] == {}
    assert "profile" in controller.hotkey_manager.families


def test_transform_shortcut_dispatches_end_to_end_on_windows(monkeypatch):
    module = _load_windows_backend({"ctrl", "alt"})
    manager = module.HotkeyManager()
    controller = _Controller()
    controller.hotkey_manager = manager
    runtime = HotkeyRuntime(controller)
    monkeypatch.setattr(runtime_module.settings_manager, "load_all_settings", lambda: {})
    monkeypatch.setattr(
        runtime_module.text_transforms,
        "load_transforms",
        lambda _settings: [Transform("polish", "Polish", "Polish it.", "ctrl+alt+p")],
    )
    runtime.refresh_profile_hotkeys()
    manager._handle_keyboard_event(_key_event("down", "p", keypad=False))
    assert _wait_until(lambda: controller.transform_requested.emitted == ["polish"])


def test_marked_windows_count_as_focused_for_window_shortcuts(_session_qt_application):
    from PyQt6.QtWidgets import QWidget

    main, scratchpad, other = QWidget(), QWidget(), QWidget()
    scratchpad.setProperty(runtime_module.HOTKEY_WINDOW_PROPERTY, True)
    assert is_hotkey_window(main, main)
    assert is_hotkey_window(scratchpad, main)
    assert not is_hotkey_window(other, main)
    assert not is_hotkey_window(None, main)
