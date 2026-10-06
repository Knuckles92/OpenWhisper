"""The Paste original shortcut pastes only after its own modifiers are up."""

import threading
import time
from types import SimpleNamespace

import pytest

import services.settings  # noqa: F401  (bind the real settings before stubbed configs)
from services import synthetic_keys
from services.runtime import hotkeys as runtime_module
from services.runtime.hotkeys import HotkeyRuntime
from tests.test_personalize_s6_backends import _windows_manager
from tests.test_push_hold_recording import _key_event


class _Signal:
    def __init__(self):
        self.emitted = []
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        self.emitted.append(args)
        for slot in self._slots:
            slot(*args)


def _controller():
    return SimpleNamespace(
        hotkey_manager=None,
        recorder=SimpleNamespace(is_recording=False),
        status_update=_Signal(),
        paste_last_original_requested=_Signal(),
        scratchpad_toggle_requested=_Signal(),
        cycle_language_requested=_Signal(),
        recording_state_changed=_Signal(),
        toggle_recording=lambda: None,
        cancel=lambda: None,
        minimize_to_tray=lambda: None,
        toggle_meeting_mode=lambda: None,
        update_status_with_auto_hide=lambda *_a: None,
        ui_controller=SimpleNamespace(update_hotkey_display=lambda _h: None),
    )


@pytest.fixture
def runtime():
    instance = HotkeyRuntime(_controller())
    yield instance
    instance.cleanup()


def test_the_shortcut_waits_for_its_modifiers_before_pasting(runtime, monkeypatch):
    held = set()
    monkeypatch.setattr(synthetic_keys, "_modifier_probe", lambda: (lambda: bool(held)))
    _module, manager = _windows_manager(held=held)
    manager.set_callbacks(on_paste_last_original=runtime._paste_last_original_after_keys_up)
    held_at_paste = []
    pasted = threading.Event()

    def paste():
        held_at_paste.append(sorted(held))
        pasted.set()

    runtime.controller.paste_last_original_requested.connect(paste)

    held.update({"ctrl", "alt"})
    assert manager._handle_keyboard_event(_key_event("down", "o", keypad=False)) is False
    assert not pasted.wait(0.1)
    held.clear()

    assert pasted.wait(2)
    assert held_at_paste == [[]]


def test_keys_still_held_at_the_deadline_paste_nothing_and_say_why(runtime, monkeypatch):
    monkeypatch.setattr(synthetic_keys, "wait_for_modifiers_released", lambda *_a, **_k: False)

    runtime._paste_last_original_after_keys_up()

    assert runtime.controller.paste_last_original_requested.emitted == []
    assert runtime.controller.status_update.emitted == [(runtime_module.KEYS_STILL_HELD,)]


def test_setup_wires_the_shortcut_through_the_wait(runtime, monkeypatch):
    calls = []

    class _Manager:
        def __init__(self, hotkeys):
            self.hotkeys = hotkeys

        def set_record_mode(self, _mode):
            pass

        def set_callbacks(self, **callbacks):
            self.callbacks = callbacks

    monkeypatch.setattr(runtime_module, "HotkeyManager", _Manager)
    for name in ("refresh_profile_hotkeys", "_install_active_window_hotkey_filter",
                 "_check_autopaste_permission"):
        monkeypatch.setattr(runtime, name, lambda: None)
    monkeypatch.setattr(
        synthetic_keys, "wait_for_modifiers_released",
        lambda *_a, **_k: calls.append("wait") or True,
    )
    runtime.controller.paste_last_original_requested.connect(lambda: calls.append("paste"))

    runtime.setup_hotkeys()
    runtime.controller.hotkey_manager.callbacks["on_paste_last_original"]()

    assert calls == ["wait", "paste"]


def test_the_wait_runs_on_the_press_thread_not_the_caller(runtime, monkeypatch):
    # Both backends spawn a thread per press; the wait blocks only that one.
    threads = []
    monkeypatch.setattr(
        synthetic_keys, "wait_for_modifiers_released",
        lambda *_a, **_k: threads.append(threading.current_thread()) or True,
    )
    _module, manager = _windows_manager(held={"ctrl", "alt"})
    manager.set_callbacks(on_paste_last_original=runtime._paste_last_original_after_keys_up)

    manager._handle_keyboard_event(_key_event("down", "o", keypad=False))
    deadline = time.monotonic() + 2
    while not threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert threads and threads[0] is not threading.current_thread()
