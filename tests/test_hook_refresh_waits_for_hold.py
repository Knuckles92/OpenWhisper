"""The periodic hook refresh waits while a record or Command Mode key is held.

A rehook forgets keyboard holds, so a refresh in the middle of a push-and-hold
used to drop its release and leave the recording running (issue #38).
"""

import time
from unittest.mock import patch

import pytest

import services.settings  # noqa: F401  (bind the real settings before stubbed configs)
from services.runtime.hotkeys import HotkeyRuntime
from services.settings import RecordingTriggerMode
from tests.test_personalize_fix2_g1_rehook_hold import _wired_windows_manager
from tests.test_personalize_s6_backends import _windows_manager
from tests.test_personalize_s6_runtime import _Controller, _drain
from tests.test_push_hold_recording import _key_event


@pytest.fixture
def runtime():
    controller = _Controller()
    controller._watchdog_interval_ms = 10_000
    controller._sleep_gap_threshold_sec = 30.0
    controller._expected_watchdog_time = time.monotonic() + 10.0
    controller._last_rehook_time = 0.0
    instance = HotkeyRuntime(controller)
    controller.runtime = instance
    yield instance
    instance.cleanup()


def test_refresh_waits_for_the_held_record_key_then_runs(runtime):
    manager = _wired_windows_manager(runtime)
    down, up = _key_event("down", "*"), _key_event("up", "*")
    assert manager._handle_keyboard_event(down) is False
    _drain(runtime)

    with patch.object(manager, "rehook", wraps=manager.rehook) as rehook:
        runtime.on_periodic_hook_refresh()
        assert rehook.call_count == 0
        time.sleep(0.3)
        assert manager._handle_keyboard_event(up) is False
        _drain(runtime)
        assert runtime.controller.calls == ["start", "stop"]

        runtime.on_watchdog_tick()
        assert rehook.call_count == 1
    assert runtime._refresh_pending is False


def test_refresh_waits_for_a_held_command_key(runtime):
    manager = _wired_windows_manager(runtime, command_mode="kp /")
    down, up = _key_event("down", "/"), _key_event("up", "/")
    assert manager._handle_keyboard_event(down) is False

    with patch.object(manager, "rehook", wraps=manager.rehook) as rehook:
        runtime.on_periodic_hook_refresh()
        assert rehook.call_count == 0
        assert manager._handle_keyboard_event(up) is False
        runtime.on_watchdog_tick()
        assert rehook.call_count == 1


def test_refresh_runs_when_nothing_is_held(runtime):
    manager = _wired_windows_manager(runtime)

    with patch.object(manager, "rehook", wraps=manager.rehook) as rehook:
        runtime.on_periodic_hook_refresh()

    assert rehook.call_count == 1
    assert runtime._refresh_pending is False


def test_a_hold_without_recent_repeats_is_stale():
    """No repeat for longer than the repeat window means a lost release or a dead hook."""
    _module, manager = _windows_manager()
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    manager._handle_keyboard_event(_key_event("down", "*"))
    pressed_at = manager._last_down[1]

    assert manager.keyboard_hold_active(pressed_at + 1.0) is True
    assert manager.keyboard_hold_active(pressed_at + 3.0) is False


def test_a_side_button_hold_does_not_hold_back_the_refresh(runtime):
    """Side-button holds survive a rehook, so the refresh need not wait."""
    manager = _wired_windows_manager(runtime, record_toggle="mouse4")
    manager._on_mouse_button("mouse4", True, frozenset(), time.monotonic())

    assert manager.keyboard_hold_active() is False


def test_another_key_after_the_press_ends_the_wait(runtime):
    """Windows repeats only the last key pressed, so the hold can no longer be proven."""
    manager = _wired_windows_manager(runtime)
    manager._handle_keyboard_event(_key_event("down", "*"))
    manager._handle_keyboard_event(_key_event("down", "a", keypad=False))

    assert manager.keyboard_hold_active() is False
