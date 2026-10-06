"""Windows backend: the enable/disable shortcut fires once per physical press.

Its default, Ctrl+Alt+Num*, shares Num* with the default record shortcut, and
Windows keeps repeating Num* after Ctrl and Alt are let go.
"""

import types

import pytest

import services.settings  # noqa: F401  (bind the real settings before stubbed configs)
from services.settings import RecordingTriggerMode
from tests.test_personalize_s6_backends import _windows_manager

SC_CTRL = 29
SC_ALT = 56
SC_STAR = 55
SC_H = 35


def _ev(event_type, name, scan_code, keypad=False):
    return types.SimpleNamespace(
        event_type=event_type, name=name, scan_code=scan_code, is_keypad=keypad
    )


STAR_DOWN = _ev("down", "*", SC_STAR, keypad=True)
STAR_UP = _ev("up", "*", SC_STAR, keypad=True)


def _manager(held, mode=RecordingTriggerMode.TOGGLE, **hotkeys):
    module, manager = _windows_manager(held=held, **hotkeys)
    module._spawn = lambda callback, *args: callback(*args)
    manager.set_record_mode(mode)
    events = []
    manager.set_callbacks(
        on_record_toggle=lambda: events.append("record_toggle"),
        on_record_press=lambda at: events.append("record_press"),
        on_record_release=lambda at: events.append("record_release"),
        on_status_update_auto_hide=events.append,
    )
    return manager, events


def _modifiers(manager, held, event_type):
    for name, code in (("ctrl", SC_CTRL), ("alt", SC_ALT)):
        if event_type == "down":
            held.add(name)
        else:
            held.discard(name)
        assert manager._handle_keyboard_event(_ev(event_type, name, code))


@pytest.mark.parametrize("mode", [RecordingTriggerMode.TOGGLE, RecordingTriggerMode.PUSH_HOLD])
def test_holding_the_shortcut_disables_once_and_never_records(mode):
    held = set()
    manager, events = _manager(held, mode)

    _modifiers(manager, held, "down")
    for _ in range(4):
        assert not manager._handle_keyboard_event(STAR_DOWN)
    _modifiers(manager, held, "up")
    for _ in range(3):
        assert not manager._handle_keyboard_event(STAR_DOWN)
    assert not manager._handle_keyboard_event(STAR_UP)

    assert events == ["STT Disabled"]
    assert manager.program_enabled is False
    assert manager._record_key_held is False
    assert manager._press_held == {}

    _modifiers(manager, held, "down")
    assert not manager._handle_keyboard_event(STAR_DOWN)
    assert events == ["STT Disabled", "STT Enabled"]


def test_separate_presses_still_toggle_and_num_star_records_again():
    held = set()
    manager, events = _manager(held)

    _modifiers(manager, held, "down")
    for _ in range(2):
        assert not manager._handle_keyboard_event(STAR_DOWN)
        assert not manager._handle_keyboard_event(STAR_UP)
    _modifiers(manager, held, "up")
    assert manager._handle_keyboard_event(_ev("down", "h", SC_H))
    assert manager._handle_keyboard_event(_ev("up", "h", SC_H))
    assert not manager._handle_keyboard_event(STAR_DOWN)

    assert events == ["STT Disabled", "STT Enabled", "record_toggle"]


def test_num_star_reaches_the_app_while_dictation_is_off():
    held = set()
    manager, events = _manager(held)
    _modifiers(manager, held, "down")
    assert not manager._handle_keyboard_event(STAR_DOWN)
    assert not manager._handle_keyboard_event(STAR_UP)
    _modifiers(manager, held, "up")

    assert manager._handle_keyboard_event(_ev("down", "h", SC_H))
    assert manager._handle_keyboard_event(STAR_DOWN)
    assert manager._handle_keyboard_event(STAR_UP)
    assert events == ["STT Disabled"]


def test_a_side_button_shortcut_toggles_once_per_click():
    held = set()
    manager, events = _manager(held, enable_disable="mouse4")

    for _ in range(2):
        manager._on_mouse_button("mouse4", True, frozenset(), 0.0)
        manager._on_mouse_button("mouse4", False, frozenset(), 0.0)

    assert events == ["STT Disabled", "STT Enabled"]
    assert manager._press_held == {}
