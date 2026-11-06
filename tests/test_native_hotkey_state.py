"""Focused shortcuts use the hotkey manager's live, conflict-filtered bindings."""

from types import SimpleNamespace

from services.native_desktop.bootstrap import _hotkey_state


def _controller():
    bindings = {"record_toggle": "Control+ALT+R", "enable_disable": "CTRL+alt+e",
                "profile:active": "ctrl+1", "action:active": "alt+2",
                "unrecognized": "ctrl+q", "stale:profile": "ctrl+3", "cancel": ""}
    manager = SimpleNamespace(program_enabled=True, capture_suspended=False,
                              hotkeys=bindings, _all_hotkeys=lambda: bindings)
    return SimpleNamespace(hotkey_manager=manager)


def test_snapshot_lists_only_live_recognized_bindings_in_canonical_form():
    assert _hotkey_state(_controller()) == {
        "hotkey_chords": ["alt+2", "ctrl+1", "ctrl+alt+e", "ctrl+alt+r"],
        "hotkeys_suspended": False,
    }


def test_disabled_program_retains_only_enable_shortcut():
    controller = _controller()
    controller.hotkey_manager.program_enabled = False
    assert _hotkey_state(controller)["hotkey_chords"] == ["ctrl+alt+e"]


def test_capture_suspension_and_absent_manager_leave_editors_unblocked():
    controller = _controller()
    controller.hotkey_manager.capture_suspended = True
    assert _hotkey_state(controller) == {"hotkey_chords": [], "hotkeys_suspended": True}
    assert _hotkey_state(SimpleNamespace(hotkey_manager=None)) == {
        "hotkey_chords": [], "hotkeys_suspended": False}
