"""X11 keypad keys reach the pynput backend as ``kp <name>`` hotkeys.

pynput's X11 listener reports numpad * as ``KeyCode(char='*')``, the same as
Shift+8, so ``kp *`` never matched outside OpenWhisper's own windows. pynput
is not installed on Windows, so the backend loads against a stub package whose
listener mimics the X11 one: a keycode-to-keysym table and an
``_event_to_key`` that folds keypad keys into characters.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import services.settings  # noqa: F401  (backend binds the real RecordingTriggerMode)

ROOT = Path(__file__).resolve().parents[1]

NUMLOCK = 0x10
CTRL, ALT = "ctrl", "alt"
KP_MULTIPLY, KP_SUBTRACT, KP_ENTER, KP_END, KP_1, KP_DECIMAL, KP_DELETE = (
    0xFFAA, 0xFFAD, 0xFF8D, 0xFF9C, 0xFFB1, 0xFFAE, 0xFF9F,
)
# keycode -> keysyms per shift level, as in a US X keymap.
KEYMAP = {
    63: [KP_MULTIPLY, KP_MULTIPLY],
    82: [KP_SUBTRACT, KP_SUBTRACT],
    104: [KP_ENTER, KP_ENTER],
    87: [KP_END, KP_1],
    91: [KP_DELETE, KP_DECIMAL],
    17: [ord("8"), ord("*")],
    38: [ord("a"), ord("A")],
}


class _Key:
    def __init__(self, name):
        self.name = name


for _name in ("cmd", "ctrl", "alt", "shift"):
    for _suffix in ("", "_l", "_r"):
        setattr(_Key, _name + _suffix, _Key(_name + _suffix))


class _KeyCode:
    def __init__(self, char=None, vk=None):
        self.char = char
        self.vk = vk

    @classmethod
    def from_vk(cls, vk):
        return cls(vk=vk)


class _XorgListener:
    """The parts of ``pynput.keyboard._xorg.Listener`` the subclass uses."""

    def __init__(self, *args, **kwargs):
        pass

    def _keycode_to_keysym(self, display, keycode, index):
        return KEYMAP[keycode][index]

    def _event_to_key(self, display, event):
        # pynput hands back the character the key types, keypad or not.
        keysym = self._keycode_to_keysym(display, event.detail, event.state & 1)
        chars = {KP_MULTIPLY: "*", KP_SUBTRACT: "-", KP_1: "1", KP_DECIMAL: ","}
        if keysym == KP_ENTER:
            return _Key("enter")
        return _KeyCode(char=chars.get(keysym, chr(keysym)), vk=keysym)


_XorgListener.__module__ = "pynput.keyboard._xorg"


@pytest.fixture
def backend():
    keyboard_ns = types.SimpleNamespace(
        Key=_Key, KeyCode=_KeyCode, Listener=_XorgListener, Controller=object
    )
    pynput_pkg = types.ModuleType("pynput")
    pynput_pkg.keyboard = keyboard_ns
    xorg_util = types.ModuleType("pynput._util.xorg")
    xorg_util.numlock_mask = lambda display: NUMLOCK
    stubs = {
        "pynput": pynput_pkg,
        "pynput.keyboard": types.ModuleType("pynput.keyboard"),
        "pynput._util": types.ModuleType("pynput._util"),
        "pynput._util.xorg": xorg_util,
    }
    spec = importlib.util.spec_from_file_location(
        "test_keypad_pynput_backend", ROOT / "services" / "_hotkey_pynput.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs), patch.object(sys, "platform", "linux"):
        spec.loader.exec_module(module)
        module._pynput_keyboard_module = keyboard_ns
        yield module


def _press(listener, keycode, state=0):
    return listener._event_to_key(None, types.SimpleNamespace(detail=keycode, state=state))


def _namer(backend):
    listener = backend.get_listener_class()()
    return lambda keycode, state=0: backend.key_to_name(_press(listener, keycode, state))


def test_x11_listener_names_keypad_keys_apart_from_the_main_keyboard(backend):
    name = _namer(backend)

    assert name(63) == "kp *"
    assert name(82) == "kp -"
    assert name(104) == "kp enter"
    # Shift+8 types the same '*' but is not the keypad key.
    assert name(17, state=1) == "*"
    assert name(38) == "a"


def test_x11_keypad_digits_follow_num_lock(backend):
    name = _namer(backend)

    assert name(87, state=NUMLOCK) == "kp 1"
    assert name(87) == "kp end"
    # pynput reports KP_Decimal as ','; the keysym says '.'.
    assert name(91, state=NUMLOCK) == "kp ."
    assert name(91) == "kp delete"


def test_keypad_press_matches_kp_hotkeys_with_and_without_modifiers(backend):
    with patch.object(backend.HotkeyManager, "_setup_keyboard_hook"):
        manager = backend.HotkeyManager(
            {
                "record_toggle": "kp *",
                "cancel": "kp -",
                "enable_disable": "ctrl+alt+kp *",
            }
        )
    listener = backend.get_listener_class()()
    fired = []
    with patch.object(manager, "trigger_action", lambda action, **kw: fired.append(action)):
        for keycode, modifiers in ((63, ()), (82, ()), (63, (CTRL, ALT))):
            manager._pressed_modifiers = set(modifiers)
            key = _press(listener, keycode)
            manager._on_press(key)
            manager._on_release(key)
        # The main-keyboard '*' is a different key.
        manager._pressed_modifiers = set()
        manager._on_press(_press(listener, 17, state=1))

    assert fired == ["record_toggle", "cancel", "enable_disable"]


def test_captured_keypad_hotkey_round_trips_through_parse(backend):
    listener = backend.get_listener_class()()
    captured = backend.format_hotkey(
        frozenset({CTRL, ALT}), backend.key_to_name(_press(listener, 63))
    )

    assert captured == "ctrl+alt+kp *"
    assert backend.parse_hotkey(captured) == (frozenset({CTRL, ALT}), "kp *")


def test_non_x11_listener_is_left_alone(backend):
    class _OtherListener(_XorgListener):
        pass

    _OtherListener.__module__ = "pynput.keyboard._uinput"
    backend._pynput_keyboard_module.Listener = _OtherListener

    assert backend.get_listener_class() is _OtherListener
