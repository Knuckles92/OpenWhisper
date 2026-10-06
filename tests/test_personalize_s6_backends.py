"""Shortcut backends: new actions, runtime families, side mouse buttons."""

import ctypes
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import services.settings  # noqa: F401  (bind the real settings before stubbed configs)
from services import _hotkey_common, _mouse_hook_win
from services.settings import RecordingTriggerMode
from tests.test_push_hold_recording import (
    SETTLE_SECONDS,
    _CallbackLog,
    _key_event,
    _load_pynput_backend,
    _load_windows_backend,
)

ROOT = Path(__file__).resolve().parents[1]

NEW_ACTIONS = {
    "command_mode": "ctrl+alt+k",
    "scratchpad_toggle": "ctrl+alt+s",
    "cycle_language": "ctrl+alt+l",
    "paste_last_original": "ctrl+alt+o",
}


def _windows_hotkeys(**overrides):
    return {
        "record_toggle": "kp *",
        "cancel": "kp -",
        "enable_disable": "ctrl+alt+kp *",
        "minimize_tray": "ctrl+alt+m",
        "meeting_toggle": "",
        **NEW_ACTIONS,
        **overrides,
    }


class _FakeMouseHook:
    def __init__(self, claims, on_button):
        self.claims = claims
        self.on_button = on_button
        self.running = False
        self.starts = 0

    def start(self):
        if not self.running:
            self.starts += 1
        self.running = True

    def stop(self):
        self.running = False


def _windows_manager(held=None, **overrides):
    module = _load_windows_backend(held if held is not None else set())
    module.MouseButtonHook = _FakeMouseHook
    return module, module.HotkeyManager(_windows_hotkeys(**overrides))


# Shared parsing and display


def test_side_buttons_parse_as_main_keys_and_display_by_number():
    keyboard_module = _load_windows_backend()
    assert keyboard_module.parse_hotkey("ctrl+mouse4") == (frozenset({"ctrl"}), "mouse4")
    assert keyboard_module.format_hotkey_display("mouse5") == "Mouse 5"
    assert keyboard_module.format_hotkey_display("ctrl+shift+mouse4") == "Ctrl+Shift+Mouse 4"

    pynput_module = _load_pynput_backend()
    assert pynput_module.parse_hotkey("alt+mouse5") == (frozenset({"alt"}), "mouse5")
    with patch.object(pynput_module.sys, "platform", "linux"):
        assert pynput_module.format_hotkey_display("ctrl+mouse4") == "Ctrl+Mouse 4"
    assert _hotkey_common.is_mouse_key("mouse4")
    assert not _hotkey_common.is_mouse_key("m")


def test_windows_key_spelled_by_keyboard_library_matches_as_win():
    module, manager = _windows_manager(held={"win"}, record_toggle="windows+f9")
    assert module.parse_hotkey("windows+f9") == (frozenset({"win"}), "f9")
    assert module.format_hotkey_display("windows+f9") == "Win+F9"
    toggles = _CallbackLog()
    manager.on_record_toggle = toggles

    assert manager._handle_keyboard_event(_key_event("down", "f9", keypad=False)) is False
    assert toggles.wait()


# Windows backend


def test_windows_press_actions_fire_their_own_callbacks():
    held = {"ctrl", "alt"}
    _module, manager = _windows_manager(held=held)
    logs = {name: _CallbackLog() for name in ("scratchpad", "language", "original")}
    manager.set_callbacks(
        on_scratchpad_toggle=logs["scratchpad"],
        on_cycle_language=logs["language"],
        on_paste_last_original=logs["original"],
    )

    for key, name in (("s", "scratchpad"), ("l", "language"), ("o", "original")):
        assert manager._handle_keyboard_event(_key_event("down", key, keypad=False)) is False
        assert logs[name].wait()
    assert manager._handle_keyboard_event(_key_event("down", "q", keypad=False)) is True


def test_windows_stub_hotkeys_without_new_actions_still_dispatch():
    module = _load_windows_backend()
    manager = module.HotkeyManager()
    assert "command_mode" not in manager.hotkeys
    cancels = _CallbackLog()
    manager.on_cancel = cancels
    assert manager._handle_keyboard_event(_key_event("down", "-")) is False
    assert cancels.wait()
    assert manager._handle_keyboard_event(_key_event("down", "x", keypad=False)) is True


def test_windows_command_mode_sends_stamped_press_and_release_once():
    held = {"ctrl", "alt"}
    module, manager = _windows_manager(held=held)
    presses, releases = _CallbackLog(), _CallbackLog()
    manager.set_callbacks(on_command_press=presses, on_command_release=releases)
    down = _key_event("down", "k", keypad=False)
    up = _key_event("up", "k", keypad=False)

    with patch.object(module.time, "monotonic", return_value=5.0):
        assert manager._handle_keyboard_event(down) is False
        assert manager._handle_keyboard_event(down) is False  # auto-repeat
    held.clear()  # modifiers let go first
    with patch.object(module.time, "monotonic", return_value=6.0):
        assert manager._handle_keyboard_event(up) is False

    assert presses.calls == [(5.0,)]
    assert releases.calls == [(6.0,)]
    assert manager._handle_keyboard_event(up) is True


def test_windows_command_release_survives_disabling_hotkeys_mid_hold():
    held = {"ctrl", "alt"}
    _module, manager = _windows_manager(held=held)
    releases = _CallbackLog()
    manager.set_callbacks(on_command_press=lambda _at: None, on_command_release=releases)
    manager._handle_keyboard_event(_key_event("down", "k", keypad=False))
    manager.program_enabled = False
    assert manager._handle_keyboard_event(_key_event("up", "k", keypad=False)) is False
    assert releases.count() == 1


def test_windows_push_hold_press_runs_inside_the_hook_with_its_time():
    module, manager = _windows_manager()
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    seen = []
    manager.set_callbacks(
        on_record_press=lambda at: seen.append(("press", at, threading.current_thread())),
        on_record_release=lambda at: seen.append(("release", at, threading.current_thread())),
    )
    with patch.object(module.time, "monotonic", side_effect=[1.0, 1.1]):
        manager._handle_keyboard_event(_key_event("down", "*"))
        manager._handle_keyboard_event(_key_event("up", "*"))
    me = threading.current_thread()
    assert seen == [("press", 1.0, me), ("release", 1.1, me)]


def test_windows_transform_family_dispatches_beside_profiles():
    held = {"ctrl", "alt"}
    _module, manager = _windows_manager(held=held)
    profiles, transforms = _CallbackLog(), _CallbackLog()
    manager.set_profile_hotkeys({"ticket": "ctrl+alt+t"}, profiles)
    manager.set_dynamic_hotkeys("transform", {"polish": "ctrl+alt+p"}, transforms)
    manager.set_profile_hotkeys({"ticket": "ctrl+alt+t"}, profiles)

    assert manager._all_hotkeys()["transform:polish"] == "ctrl+alt+p"
    down = _key_event("down", "p", keypad=False)
    assert manager._handle_keyboard_event(down) is False
    assert transforms.wait()
    manager._handle_keyboard_event(down)
    assert manager._handle_keyboard_event(_key_event("up", "p", keypad=False)) is False
    assert transforms.calls == [("polish",)]

    manager.set_dynamic_hotkeys("transform", {}, transforms)
    assert manager._handle_keyboard_event(down) is True
    with pytest.raises(ValueError):
        manager.set_dynamic_hotkeys("macro", {}, transforms)


def test_windows_mouse_hook_runs_only_while_a_side_button_is_bound():
    _module, manager = _windows_manager()
    hook = manager._mouse_hook
    assert not hook.running

    manager.update_hotkeys({"scratchpad_toggle": "mouse4"})
    assert hook.running
    manager.set_capture_suspended(True)
    assert not hook.running
    manager.set_capture_suspended(False)
    assert hook.running
    starts = hook.starts
    manager.rehook()
    assert hook.running and hook.starts == starts + 1
    manager.update_hotkeys({"scratchpad_toggle": ""})
    assert not hook.running

    manager.set_dynamic_hotkeys("transform", {"polish": "ctrl+mouse5"}, lambda _id: None)
    assert hook.running
    manager.cleanup()
    assert not hook.running


def test_windows_side_button_claims_follow_bindings_and_enabled_state():
    _module, manager = _windows_manager(record_toggle="mouse4", enable_disable="ctrl+mouse5")
    claims = manager._claims_mouse_button
    assert claims("mouse4", frozenset())
    assert not claims("mouse4", frozenset({"ctrl"}))
    assert not claims("mouse5", frozenset())
    manager.program_enabled = False
    assert not claims("mouse4", frozenset())
    assert claims("mouse5", frozenset({"ctrl"}))
    manager.program_enabled = True
    manager.set_capture_suspended(True)
    assert not claims("mouse4", frozenset())


def test_windows_side_button_drives_push_and_hold():
    _module, manager = _windows_manager(record_toggle="mouse4")
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    events = []
    manager.set_callbacks(
        on_record_press=lambda at: events.append(("press", at)),
        on_record_release=lambda at: events.append(("release", at)),
    )
    manager._on_mouse_button("mouse4", True, frozenset(), 3.0)
    manager._on_mouse_button("mouse4", True, frozenset(), 3.05)
    manager._on_mouse_button("mouse4", False, frozenset(), 3.4)
    assert events == [("press", 3.0), ("release", 3.4)]


# Windows mouse hook, against a fake user32


class _FakeUser32:
    def __init__(self):
        self.pressed = set()
        self.next_calls = []
        self.quit = threading.Event()
        self.unhooked = []
        self.posted = []

    def CallNextHookEx(self, hook, code, wparam, lparam):
        self.next_calls.append(wparam)
        return 0

    def GetAsyncKeyState(self, key):
        return -32768 if key in self.pressed else 0

    def PeekMessageW(self, *args):
        return 0

    def SetWindowsHookExW(self, kind, proc, module, thread):
        assert kind == _mouse_hook_win.WH_MOUSE_LL
        self.proc = proc
        return 0xBEEF

    def GetMessageW(self, *args):
        self.quit.wait(5)
        return 0

    def PostThreadMessageW(self, thread_id, message, wparam, lparam):
        self.posted.append((thread_id, message))
        self.quit.set()
        return 1

    def UnhookWindowsHookEx(self, handle):
        self.unhooked.append(handle)
        return 1


class _FakeKernel32:
    def GetCurrentThreadId(self):
        return 77

    def GetModuleHandleW(self, _name):
        return 1


def _mouse_event(button=1, flags=0):
    info = _mouse_hook_win.MSLLHOOKSTRUCT()
    info.mouseData = button << 16
    info.flags = flags
    return info


class _HookHarness:
    def __init__(self, claimed=("mouse4", "mouse5")):
        self.user32 = _FakeUser32()
        self.asked = []
        self.claimed = set(claimed)
        self.hook = _mouse_hook_win.MouseButtonHook(
            self._claims, Mock(),
            libraries=(self.user32, _FakeKernel32(), lambda function: function),
        )
        self.events = _mouse_hook_win.queue.Queue()
        self.callback = self.hook._make_callback(self.events)
        self._structs = []

    def _claims(self, button, modifiers):
        self.asked.append((button, modifiers))
        return button in self.claimed

    def send(self, message, button=1, flags=0):
        info = _mouse_event(button, flags)
        self._structs.append(info)
        return self.callback(0, message, ctypes.addressof(info))

    def drained(self):
        items = []
        while not self.events.empty():
            items.append(self.events.get_nowait())
        return items


def test_mouse_hook_passes_everything_but_side_buttons_untouched():
    harness = _HookHarness()
    for message in (0x0200, 0x0201, 0x0202, 0x020A):  # move, left down/up, wheel
        assert harness.callback(0, message, 0) == 0
    assert harness.user32.next_calls == [0x0200, 0x0201, 0x0202, 0x020A]
    assert harness.asked == []
    assert harness.drained() == []


def test_mouse_hook_suppresses_a_claimed_press_and_always_its_release():
    harness = _HookHarness()
    harness.user32.pressed = {0x11}
    assert harness.send(_mouse_hook_win.WM_XBUTTONDOWN, button=2) == 1
    harness.claimed.clear()  # the binding goes away mid-press
    assert harness.send(_mouse_hook_win.WM_XBUTTONUP, button=2) == 1
    assert harness.send(_mouse_hook_win.WM_XBUTTONUP, button=2) == 0

    (down, up) = harness.drained()
    assert down[:3] == ("mouse5", True, frozenset({"ctrl"}))
    assert up[:2] == ("mouse5", False)
    assert down[3] <= up[3]
    assert harness.asked == [("mouse5", frozenset({"ctrl"}))]
    assert harness.hook.worst_callback_ms > 0


def test_mouse_hook_leaves_unclaimed_and_injected_buttons_to_the_app():
    harness = _HookHarness(claimed=())
    assert harness.send(_mouse_hook_win.WM_XBUTTONDOWN) == 0
    assert harness.send(_mouse_hook_win.WM_XBUTTONUP) == 0
    harness.claimed.add("mouse4")
    assert harness.send(_mouse_hook_win.WM_XBUTTONDOWN, flags=_mouse_hook_win.LLMHF_INJECTED) == 0
    assert harness.send(_mouse_hook_win.WM_XBUTTONDOWN, button=3) == 0
    assert harness.drained() == []
    assert harness.asked == [("mouse4", frozenset())]


def test_mouse_hook_thread_installs_dispatches_and_unhooks(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "services.diagnostics.record_metrics", lambda **values: recorded.append(values)
    )
    user32 = _FakeUser32()
    delivered = _CallbackLog()
    hook = _mouse_hook_win.MouseButtonHook(
        lambda button, modifiers: True, delivered,
        libraries=(user32, _FakeKernel32(), lambda function: function),
    )
    hook.start()
    try:
        deadline = time.monotonic() + 2
        while not hasattr(user32, "proc") and time.monotonic() < deadline:
            time.sleep(0.01)
        info = _mouse_event(1)
        assert user32.proc(0, _mouse_hook_win.WM_XBUTTONDOWN, ctypes.addressof(info)) == 1
        assert delivered.wait()
        assert delivered.calls[0][:3] == ("mouse4", True, frozenset())
        assert hook.running
    finally:
        hook.stop()
    assert user32.posted == [(77, _mouse_hook_win.WM_QUIT)]
    assert user32.unhooked == [0xBEEF]
    assert not hook.running
    assert recorded and "mouse_hook_callback_ms" in recorded[0]


# pynput backend (macOS Carbon, X11, Hyprland D-Bus, Qt fallback)

PYNPUT_HOTKEYS = {
    "record_toggle": "ctrl+alt+r",
    "cancel": "ctrl+alt+escape",
    "enable_disable": "ctrl+alt+shift+r",
    "minimize_tray": "ctrl+alt+m",
    "meeting_toggle": "",
    **NEW_ACTIONS,
}
CTRL_ALT = frozenset({"ctrl", "alt"})


def _pynput_manager(**overrides):
    module = _load_pynput_backend()
    with patch.object(module.HotkeyManager, "_setup_keyboard_hook"):
        manager = module.HotkeyManager({**PYNPUT_HOTKEYS, **overrides})
    return module, manager


def test_pynput_press_actions_fire_their_own_callbacks():
    _module, manager = _pynput_manager()
    logs = {name: _CallbackLog() for name in ("scratchpad", "language", "original")}
    manager.set_callbacks(
        on_scratchpad_toggle=logs["scratchpad"],
        on_cycle_language=logs["language"],
        on_paste_last_original=logs["original"],
    )
    assert manager.handle_hotkey_press(CTRL_ALT, "s")
    assert manager.handle_hotkey_press(CTRL_ALT, "l", source="qt")
    manager.trigger_action("paste_last_original")  # Carbon / Hyprland shape
    for log in logs.values():
        assert log.wait()
    assert not manager.handle_hotkey_press(CTRL_ALT, "q")


def test_pynput_command_mode_forwards_per_key_press_and_release():
    _module, manager = _pynput_manager()
    presses, releases = _CallbackLog(), _CallbackLog()
    manager.set_callbacks(on_command_press=presses, on_command_release=releases)

    assert manager.handle_hotkey_press(CTRL_ALT, "k", source="qt")
    assert not manager.handle_hotkey_press(CTRL_ALT, "x", source="qt")
    # Modifiers released first; the main key still ends the hold.
    assert manager.handle_hotkey_release(frozenset(), "k", source="qt")
    assert not manager.handle_hotkey_release(frozenset(), "k", source="qt")

    assert presses.count() == 1 and releases.count() == 1
    assert all(isinstance(call[0], float) for call in presses.calls + releases.calls)


def test_pynput_command_mode_forwards_carbon_and_hyprland_released_events():
    _module, manager = _pynput_manager()
    presses, releases = _CallbackLog(), _CallbackLog()
    manager.set_callbacks(on_command_press=presses, on_command_release=releases)

    manager.trigger_action("command_mode")
    manager.trigger_action("command_mode")  # still held
    manager.trigger_action("command_mode", released=True)
    manager.trigger_action("command_mode", released=True)  # no press to end
    manager.trigger_action("command_mode")

    assert presses.count() == 2 and releases.count() == 1


def test_pynput_command_release_survives_disabling_hotkeys_mid_hold():
    _module, manager = _pynput_manager()
    releases = _CallbackLog()
    manager.set_callbacks(on_command_press=lambda _at: None, on_command_release=releases)
    manager.trigger_action("command_mode")
    manager.program_enabled = False
    manager.trigger_action("command_mode")
    manager.trigger_action("command_mode", released=True)
    assert releases.count() == 1


def test_pynput_dedupe_drops_only_a_second_sources_copy():
    _module, manager = _pynput_manager()
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    presses = _CallbackLog()
    manager.set_callbacks(on_record_press=presses, on_record_release=lambda _at: None)

    assert manager.handle_hotkey_press(CTRL_ALT, "r", source="global")
    assert manager.handle_hotkey_press(CTRL_ALT, "r", source="qt")  # same press, seen twice
    assert manager.handle_hotkey_release(frozenset(), "r", source="global")
    assert manager.handle_hotkey_press(CTRL_ALT, "r", source="global")  # a real second tap
    assert presses.count() == 2


def test_pynput_double_tap_reaches_the_runtime_from_the_listener():
    module, manager = _pynput_manager()
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    events = []
    manager.set_callbacks(
        on_record_press=lambda _at: events.append("press"),
        on_record_release=lambda _at: events.append("release"),
    )
    key = module.pynput_keyboard
    manager._on_press(key.Key.ctrl)
    manager._on_press(key.Key.alt)
    for _ in range(2):
        manager._on_press(key.KeyCode(char="r"))
        manager._on_release(key.KeyCode(char="r"))
    assert events == ["press", "release", "press", "release"]


def test_pynput_transform_family_reaches_carbon_and_dispatches():
    _module, manager = _pynput_manager()
    manager._carbon_registrar = Mock()
    transforms = _CallbackLog()
    manager.set_profile_hotkeys({"ticket": "ctrl+alt+t"}, lambda _id: None)
    manager.set_dynamic_hotkeys("transform", {"polish": "ctrl+alt+p"}, transforms)
    registered = manager._carbon_registrar.register_hotkeys.call_args.args[0]
    assert registered["transform:polish"] == "ctrl+alt+p"
    assert registered["profile:ticket"] == "ctrl+alt+t"

    assert manager.handle_hotkey_press(CTRL_ALT, "p")
    assert transforms.wait()
    manager.trigger_action("transform:polish")  # still held
    assert manager.handle_hotkey_release(frozenset(), "p")
    manager._last_action_times.clear()
    manager.trigger_action("transform:polish")
    manager.trigger_action("transform:polish", released=True)
    time.sleep(SETTLE_SECONDS)
    assert transforms.calls == [("polish",), ("polish",)]
    manager.trigger_action("transform:missing")


class _FakeMouseListener:
    instances = []

    def __init__(self, on_click):
        self.on_click = on_click
        self.started = False
        self.stopped = False
        _FakeMouseListener.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_x11_mouse_listener_starts_only_once_a_side_button_is_bound():
    module, manager = _pynput_manager()
    _FakeMouseListener.instances = []
    module._pynput_mouse_module = types.SimpleNamespace(Listener=_FakeMouseListener)
    manager._listener = object()  # the X11 keyboard listener is running
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    presses, releases = _CallbackLog(), _CallbackLog()
    manager.set_callbacks(on_record_press=presses, on_record_release=releases)

    with patch.object(module, "mouse_shortcuts_supported", return_value=True):
        manager.update_hotkeys({"cancel": "ctrl+alt+escape"})
        assert _FakeMouseListener.instances == []
        manager.update_hotkeys({"record_toggle": "mouse4"})
        (listener,) = _FakeMouseListener.instances
        assert listener.started

        back = types.SimpleNamespace(name="button8")
        listener.on_click(0, 0, types.SimpleNamespace(name="left"), True)
        listener.on_click(0, 0, back, True)
        listener.on_click(0, 0, back, False)
        assert presses.count() == 1 and releases.count() == 1

        manager.set_capture_suspended(True)
        assert listener.stopped and manager._mouse_listener is None
        manager.set_capture_suspended(False)
        assert len(_FakeMouseListener.instances) == 2
        manager.cleanup()
        assert _FakeMouseListener.instances[-1].stopped


def test_mouse_listener_is_never_loaded_where_side_buttons_are_unsupported():
    module, manager = _pynput_manager()
    manager._listener = object()
    with patch.object(module, "mouse_shortcuts_supported", return_value=False), patch.object(
        module, "_load_pynput_mouse"
    ) as load:
        manager.update_hotkeys({"record_toggle": "mouse4"})
    load.assert_not_called()


def test_mouse_shortcut_support_by_desktop():
    module = _load_pynput_backend()
    with patch.object(module.sys, "platform", "darwin"):
        assert not module.mouse_shortcuts_supported()
    with patch.object(module.sys, "platform", "linux"), patch.object(
        module, "is_wayland_session", return_value=True
    ):
        assert not module.mouse_shortcuts_supported()
    with patch.object(module.sys, "platform", "linux"), patch.object(
        module, "is_wayland_session", return_value=False
    ):
        assert module.mouse_shortcuts_supported()
    assert _load_windows_backend().mouse_shortcuts_supported()


# Carbon and Hyprland refuse side buttons


class _FakeCarbonLibrary:
    def __init__(self):
        self.registered = []

    def GetApplicationEventTarget(self):
        return 1

    def InstallEventHandler(self, *args):
        return 0

    def RegisterEventHotKey(self, keycode, modifiers, hotkey_id, target, options, out_ref):
        self.registered.append(keycode)
        return 0

    def UnregisterEventHotKey(self, _ref):
        return 0


def _load_carbon_module():
    keyboard_stub = types.SimpleNamespace(Key=[])
    pynput_pkg = types.ModuleType("pynput")
    pynput_pkg.keyboard = keyboard_stub
    spec = importlib.util.spec_from_file_location(
        "test_personalize_s6_carbon", ROOT / "services" / "_hotkey_carbon.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"pynput": pynput_pkg, "pynput.keyboard": keyboard_stub}):
        spec.loader.exec_module(module)
    return module


def test_carbon_skips_side_buttons_and_unset_actions():
    carbon = _load_carbon_module()
    library = _FakeCarbonLibrary()
    carbon._carbon = library
    registrar = carbon.CarbonHotkeyRegistrar(on_action=lambda *_: None)
    registrar.register_hotkeys(
        {"record_toggle": "ctrl+alt+r", "command_mode": "mouse4", "cycle_language": ""}
    )
    assert list(registrar._id_to_action.values()) == ["record_toggle"]
    assert library.registered == [carbon.keycode_for("r")]


@pytest.mark.parametrize("hotkey", ["mouse4", "ctrl+mouse5"])
def test_hyprland_refuses_side_buttons(hotkey):
    from services.omarchy_controls import binding_key

    with pytest.raises(ValueError, match="Unsupported shortcut"):
        binding_key(hotkey)
