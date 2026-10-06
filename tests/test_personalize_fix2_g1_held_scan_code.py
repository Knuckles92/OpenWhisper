"""Windows backend: holds follow the physical key, whatever Shift does to its name.

The ``keyboard`` library names each event from the Shift state at that moment,
so Ctrl+Shift+1 goes down as "!" and, when Shift is let go first, comes up as
"1". Auto-repeats and the key-up keep the key's scan code.
"""

import types

import services.settings  # noqa: F401  (bind the real settings before stubbed configs)
from services.settings import RecordingTriggerMode
from tests.test_personalize_s6_backends import _windows_manager

SC_1 = 2
SC_CTRL = 29
SC_SHIFT = 42
SC_ALT = 56
SC_M = 50
SC_H = 35
SC_O = 24
SC_T = 20
SC_STAR = 55
SC_SLASH = 53


def _ev(event_type, name, scan_code, keypad=False):
    return types.SimpleNamespace(
        event_type=event_type, name=name, scan_code=scan_code, is_keypad=keypad
    )


def _manager(held, mode=RecordingTriggerMode.PUSH_HOLD, **hotkeys):
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
        on_minimize_tray=lambda: events.append("minimize"),
        on_scratchpad_toggle=lambda: events.append("scratchpad"),
        on_paste_last_original=lambda: events.append("paste_original"),
    )
    return manager, events


def _passes(manager, event) -> bool:
    """True when the hook lets the event through to the focused app."""
    return manager._handle_keyboard_event(event)


def _press_modifiers(manager, held, *names):
    codes = {"ctrl": SC_CTRL, "shift": SC_SHIFT, "alt": SC_ALT}
    for name in names:
        held.add(name)
        assert _passes(manager, _ev("down", name, codes[name]))


def _release_modifiers(manager, held, *names):
    codes = {"ctrl": SC_CTRL, "shift": SC_SHIFT, "alt": SC_ALT}
    for name in names:
        held.discard(name)
        assert _passes(manager, _ev("up", name, codes[name]))


def test_shift_symbol_shortcut_fires_again_when_shift_is_let_go_first():
    held = set()
    manager, events = _manager(held, scratchpad_toggle="ctrl+shift+!")

    for _ in range(2):
        _press_modifiers(manager, held, "ctrl", "shift")
        assert not _passes(manager, _ev("down", "!", SC_1))
        _release_modifiers(manager, held, "shift", "ctrl")
        # The key-up of the swallowed press is swallowed too.
        assert not _passes(manager, _ev("up", "1", SC_1))

    assert events == ["scratchpad", "scratchpad"]
    assert manager._press_held == {}


def test_typed_shift_symbol_passes_through_after_the_shortcut():
    held = set()
    manager, events = _manager(held, scratchpad_toggle="ctrl+shift+!")
    _press_modifiers(manager, held, "ctrl", "shift")
    _passes(manager, _ev("down", "!", SC_1))
    _release_modifiers(manager, held, "shift", "ctrl")
    _passes(manager, _ev("up", "1", SC_1))

    # Shift+1 typed in another app, released in either order.
    _press_modifiers(manager, held, "shift")
    assert _passes(manager, _ev("down", "!", SC_1))
    _release_modifiers(manager, held, "shift")
    assert _passes(manager, _ev("up", "1", SC_1))
    _press_modifiers(manager, held, "shift")
    assert _passes(manager, _ev("down", "!", SC_1))
    assert _passes(manager, _ev("up", "!", SC_1))

    assert events == ["scratchpad"]


def test_shift_symbol_shortcut_suppresses_its_repeats_until_the_key_comes_up():
    held = set()
    manager, events = _manager(held, scratchpad_toggle="ctrl+shift+!")
    _press_modifiers(manager, held, "ctrl", "shift")
    assert not _passes(manager, _ev("down", "!", SC_1))
    assert not _passes(manager, _ev("down", "!", SC_1))
    _release_modifiers(manager, held, "shift")
    # Windows keeps repeating the key, now named "1"; nothing reaches the app.
    assert not _passes(manager, _ev("down", "1", SC_1))
    assert not _passes(manager, _ev("up", "1", SC_1))

    assert events == ["scratchpad"]


def test_command_mode_on_a_shift_symbol_ends_when_shift_is_let_go_first():
    held = set()
    manager, events = _manager(held, record_toggle="ctrl+1", command_mode="ctrl+shift+!")
    _press_modifiers(manager, held, "ctrl", "shift")
    assert not _passes(manager, _ev("down", "!", SC_1))
    _release_modifiers(manager, held, "shift")
    # The repeat now reads Ctrl+1, the record shortcut, but is the same key.
    assert not _passes(manager, _ev("down", "1", SC_1))
    assert not _passes(manager, _ev("up", "1", SC_1))

    assert events == ["command_press", "command_release"]
    assert manager._command_key_held is False
    assert manager._record_key_held is False


def test_missed_key_up_never_swallows_later_typing():
    held = set()
    manager, events = _manager(held)
    _press_modifiers(manager, held, "ctrl", "alt")
    assert not _passes(manager, _ev("down", "m", SC_M))
    # The "m" key-up went to an elevated window; the hook never saw it.
    _release_modifiers(manager, held, "alt", "ctrl")

    assert _passes(manager, _ev("down", "h", SC_H))
    assert _passes(manager, _ev("up", "h", SC_H))
    assert _passes(manager, _ev("down", "m", SC_M))
    assert _passes(manager, _ev("up", "m", SC_M))

    _press_modifiers(manager, held, "ctrl", "alt")
    assert not _passes(manager, _ev("down", "m", SC_M))
    assert events == ["minimize", "minimize"]


def test_key_up_missed_behind_an_elevated_window_frees_the_key_later():
    held = set()
    module, manager = _windows_manager(held=held)
    module._spawn = lambda callback, *args: callback(*args)
    fired = []
    manager.set_callbacks(on_minimize_tray=lambda: fired.append("minimize"))
    clock = [100.0]
    module.time = types.SimpleNamespace(monotonic=lambda: clock[0])

    _press_modifiers(manager, held, "ctrl", "alt")
    assert not _passes(manager, _ev("down", "m", SC_M))
    # The elevated window got every key-up and the typing after it; the hook
    # saw none of it. Back in a normal app the user types "m" first.
    held.clear()
    clock[0] += 5.0
    assert _passes(manager, _ev("down", "m", SC_M))
    assert _passes(manager, _ev("up", "m", SC_M))

    assert fired == ["minimize"]


def test_slow_auto_repeat_still_counts_as_a_repeat():
    held = set()
    module, manager = _windows_manager(held=held)
    module._spawn = lambda callback, *args: callback(*args)
    fired = []
    manager.set_callbacks(on_paste_last_original=lambda: fired.append("paste_original"))
    clock = [100.0]
    module.time = types.SimpleNamespace(monotonic=lambda: clock[0])

    _press_modifiers(manager, held, "ctrl", "alt")
    assert not _passes(manager, _ev("down", "o", SC_O))
    # Filter Keys allows a 2 s repeat delay and rate.
    for _ in range(3):
        clock[0] += 2.0
        assert not _passes(manager, _ev("down", "o", SC_O))

    assert fired == ["paste_original"]


def test_missed_key_up_never_strands_a_transform_shortcut():
    held = set()
    manager, events = _manager(held)
    fired = []
    manager.set_dynamic_hotkeys("transform", {"polish": "ctrl+alt+t"}, fired.append)
    _press_modifiers(manager, held, "ctrl", "alt")
    assert not _passes(manager, _ev("down", "t", SC_T))
    _release_modifiers(manager, held, "alt", "ctrl")
    assert _passes(manager, _ev("down", "h", SC_H))
    manager._dynamic_debouncers[("transform", "polish")].reset()

    _press_modifiers(manager, held, "ctrl", "alt")
    assert not _passes(manager, _ev("down", "t", SC_T))

    assert fired == ["polish", "polish"]


def test_numpad_star_repeat_after_ctrl_is_let_go_stays_with_command_mode():
    held = set()
    manager, events = _manager(held, record_toggle="kp *", command_mode="ctrl+kp *")
    star_down = _ev("down", "*", SC_STAR, keypad=True)

    for _ in range(2):
        _press_modifiers(manager, held, "ctrl")
        assert not _passes(manager, star_down)
        assert not _passes(manager, star_down)
        _release_modifiers(manager, held, "ctrl")
        assert not _passes(manager, star_down)
        assert not _passes(manager, _ev("up", "*", SC_STAR, keypad=True))

    assert events == ["command_press", "command_release"] * 2
    assert manager._record_key_held is False


def test_paste_original_fires_once_per_physical_press():
    held = set()
    manager, events = _manager(held)
    _press_modifiers(manager, held, "ctrl", "alt")
    for _ in range(5):
        assert not _passes(manager, _ev("down", "o", SC_O))
    assert not _passes(manager, _ev("up", "o", SC_O))
    # Second physical press with the modifiers still held.
    assert not _passes(manager, _ev("down", "o", SC_O))

    assert events == ["paste_original", "paste_original"]


def test_main_key_with_the_same_scan_code_does_not_end_a_numpad_hold():
    held = set()
    manager, events = _manager(held, record_toggle="kp /")
    assert not _passes(manager, _ev("down", "/", SC_SLASH, keypad=True))
    # The main-row "/" shares the numpad key's scan code.
    assert _passes(manager, _ev("up", "/", SC_SLASH, keypad=False))
    assert manager._record_key_held is True
    assert not _passes(manager, _ev("up", "/", SC_SLASH, keypad=True))

    assert events == ["record_press", "record_release"]
