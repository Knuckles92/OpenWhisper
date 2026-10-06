"""Windows backend: auto-repeat of a held shortcut never dispatches another action."""

import pytest

import services.settings  # noqa: F401  (bind the real settings before stubbed configs)
from services.settings import RecordingTriggerMode
from tests.test_personalize_s6_backends import _windows_manager
from tests.test_push_hold_recording import _key_event


def _manager(held, mode, **hotkeys):
    module, manager = _windows_manager(held=held, **hotkeys)
    # Run spawned callbacks inline so "never fired" needs no sleep.
    module._spawn = lambda callback, *args: callback(*args)
    manager.set_record_mode(mode)
    events = []
    manager.set_callbacks(
        on_record_toggle=lambda: events.append("record_toggle"),
        on_record_press=lambda at: events.append("record_press"),
        on_record_release=lambda at: events.append("record_release"),
        on_command_press=lambda at: events.append("command_press"),
        on_command_release=lambda at: events.append("command_release"),
        on_paste_last_original=lambda: events.append("paste_original"),
    )
    return manager, events


# The user lets go of the extra modifier before the main key while Windows
# keeps auto-repeating the main key, so a repeat matches the shorter shortcut.
@pytest.mark.parametrize("mode", [RecordingTriggerMode.PUSH_HOLD, RecordingTriggerMode.TOGGLE])
@pytest.mark.parametrize(
    "record, command, key, keypad, chord, let_go_first",
    [
        ("kp *", "ctrl+kp *", "*", True, {"ctrl"}, {"ctrl"}),
        ("ctrl+space", "ctrl+shift+space", "space", False, {"ctrl", "shift"}, {"shift"}),
    ],
    ids=["numpad-star", "ctrl-space"],
)
def test_command_hold_survives_a_repeat_after_its_modifier_is_released(
    mode, record, command, key, keypad, chord, let_go_first
):
    held = set()
    manager, events = _manager(
        held, mode, record_toggle=record, command_mode=command,
        enable_disable="ctrl+alt+shift+f12",
    )
    down = _key_event("down", key, keypad=keypad)
    up = _key_event("up", key, keypad=keypad)

    held.update(chord)
    assert manager._handle_keyboard_event(down) is False
    assert manager._handle_keyboard_event(down) is False
    held.difference_update(let_go_first)
    # Still suppressed: the repeat belongs to the held Command Mode key.
    assert manager._handle_keyboard_event(down) is False
    assert manager._handle_keyboard_event(up) is False
    held.clear()

    assert events == ["command_press", "command_release"]
    assert manager._command_key_held is False
    assert manager._record_key_held is False

    held.update(chord)
    manager._handle_keyboard_event(down)
    assert events[-1] == "command_press"


def test_release_ends_every_hold_on_that_main_key():
    held = {"ctrl"}
    manager, events = _manager(
        held, RecordingTriggerMode.PUSH_HOLD, record_toggle="kp *", command_mode="ctrl+kp *",
        enable_disable="ctrl+alt+shift+f12",
    )
    # Both holds set at once, as a missed KEY_UP or rehook could leave them.
    manager._record_key_held = True
    manager._command_key_held = True

    assert manager._handle_keyboard_event(_key_event("up", "*")) is False

    assert events == ["record_release", "command_release"]
    assert not manager._record_key_held and not manager._command_key_held


def test_paste_original_fires_once_per_press_despite_auto_repeat():
    held = {"ctrl", "alt"}
    manager, events = _manager(held, RecordingTriggerMode.PUSH_HOLD)
    down = _key_event("down", "o", keypad=False)

    for _ in range(5):
        assert manager._handle_keyboard_event(down) is False
    assert events == ["paste_original"]

    held.clear()
    assert manager._handle_keyboard_event(_key_event("up", "o", keypad=False)) is False
    held.update({"ctrl", "alt"})
    manager._handle_keyboard_event(down)
    assert events == ["paste_original", "paste_original"]


def test_forgetting_held_keys_frees_a_press_action():
    held = {"ctrl", "alt"}
    manager, events = _manager(held, RecordingTriggerMode.PUSH_HOLD)
    down = _key_event("down", "o", keypad=False)
    manager._handle_keyboard_event(down)

    manager.set_capture_suspended(True)
    manager.set_capture_suspended(False)
    manager._handle_keyboard_event(down)

    assert events == ["paste_original", "paste_original"]
