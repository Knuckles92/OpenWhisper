from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import hyprland
from services.omarchy_controls import OmarchyControls, binding_key


@pytest.mark.parametrize(
    "hotkey, expected",
    [
        ("kp *", ("KP_Multiply", "KP_Multiply", 0)),
        ("ctrl+alt+kp *", ("CTRL + ALT + KP_Multiply", "KP_Multiply", 12)),
        ("super+shift+f12", ("SHIFT + SUPER + F12", "F12", 65)),
    ],
)
def test_native_binding_keys(hotkey, expected):
    assert binding_key(hotkey) == expected


def controls(hotkeys):
    manager = SimpleNamespace(_all_hotkeys=lambda: hotkeys, capture_suspended=False)
    return OmarchyControls(SimpleNamespace(hotkey_manager=manager))


def test_never_claims_or_removes_an_existing_desktop_binding(monkeypatch):
    native = controls({"record_toggle": "kp *"})
    existing = [{"key": "KP_Multiply", "modmask": 0, "description": "User's shortcut"}]
    monkeypatch.setattr(hyprland, "query", lambda _: existing)
    evaluate = Mock()
    monkeypatch.setattr(hyprland, "evaluate", evaluate)
    native._sync()
    assert "already used" in native._status
    assert not native._owned
    evaluate.assert_not_called()


def test_registers_press_and_release_and_restores_after_reload(monkeypatch):
    native = controls({"record_toggle": "ctrl+alt+r"})
    monkeypatch.setattr(hyprland, "query", lambda _: [])
    evaluate = Mock()
    monkeypatch.setattr(hyprland, "evaluate", evaluate)
    native._sync()
    assert evaluate.call_count == 2
    assert "release=false" in evaluate.call_args_list[0].args[0]
    assert "release=true" in evaluate.call_args_list[1].args[0]
    native._sync()  # Hyprland reloaded, so the binding list is empty again.
    assert evaluate.call_count == 4


def test_capture_releases_only_our_bindings(monkeypatch):
    native = controls({"record_toggle": "kp *"})
    native._owned = {"record_toggle": ("kp *", "KP_Multiply", "KP_Multiply", 0)}
    native.controller.hotkey_manager.capture_suspended = True
    monkeypatch.setattr(
        hyprland,
        "query",
        lambda _: [
            {
                "key": "KP_Multiply",
                "modmask": 0,
                "description": "OpenWhisper: record_toggle",
            },
        ],
    )
    evaluate = Mock()
    monkeypatch.setattr(hyprland, "evaluate", evaluate)
    native._sync()
    evaluate.assert_called_once_with('hl.unbind("KP_Multiply")')
    assert not native._owned


def test_unknown_dbus_action_cannot_dispatch(monkeypatch):
    native = controls({"record_toggle": "kp *"})
    native.controller.hotkey_manager.trigger_action = Mock()
    assert not native.Trigger("run-shell-command", False)
    native.controller.hotkey_manager.trigger_action.assert_not_called()


@pytest.mark.parametrize(
    "app, mods", [("org.mozilla.firefox", "CTRL"), ("foot", "CTRL SHIFT")]
)
def test_native_paste_targets_focused_app(monkeypatch, app, mods):
    monkeypatch.setattr(
        hyprland, "query", lambda _: {"address": "0x123abc", "class": app}
    )
    evaluate = Mock()
    monkeypatch.setattr(hyprland, "evaluate", evaluate)
    hyprland.send_paste()
    code = evaluate.call_args.args[0]
    assert f'mods="{mods}"' in code
    assert 'window="address:0x123abc"' in code


def test_paste_does_not_inject_into_own_ui(monkeypatch):
    monkeypatch.setattr(
        hyprland, "query", lambda _: {"address": "0x123abc", "class": "openwhisper"}
    )
    with pytest.raises(RuntimeError, match="destination"):
        hyprland.send_paste()


def test_native_control_preserves_push_hold_and_profile_release(monkeypatch):
    import threading

    from services import _hotkey_pynput
    from services.settings import RecordingTriggerMode

    monkeypatch.setattr(_hotkey_pynput, "is_wayland_session", lambda: True)
    manager = _hotkey_pynput.HotkeyManager({"record_toggle": "kp *"})
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    started, stopped = threading.Event(), threading.Event()
    manager.set_callbacks(on_record_press=started.set, on_record_release=stopped.set)
    native = OmarchyControls(SimpleNamespace(hotkey_manager=manager))
    assert native.Trigger("record_toggle", False)
    assert started.wait(1)
    assert native.Trigger("record_toggle", True)
    assert stopped.wait(1)
    manager.set_profile_hotkeys({"sample": "ctrl+alt+r"}, lambda _: None)
    native.Trigger("profile:sample", False)
    assert "sample" in manager._profile_held
    native.Trigger("profile:sample", True)
    assert not manager._profile_held
    manager.cleanup()


def test_shell_clicks_use_app_actions_even_with_shortcuts_paused():
    ui = Mock()
    native = OmarchyControls(
        SimpleNamespace(
            ui_controller=ui, hotkey_manager=SimpleNamespace(program_enabled=False)
        )
    )
    assert native.Button("record")
    assert native.Button("record")
    assert native.Button("cancel")
    assert native.Button("meeting")
    assert native.Button("show")
    assert not native.Button("run-shell-command")
    assert ui._on_tray_toggle_recording.call_count == 2
    ui.cancel_recording.assert_called_once_with()
    ui._on_tray_meeting_toggle.assert_called_once_with()
    ui.main_window.restore_from_tray.assert_called_once_with()


def test_repairs_partial_press_release_registration(monkeypatch):
    native = controls({"record_toggle": "kp *"})
    monkeypatch.setattr(
        hyprland,
        "query",
        lambda _: [
            {
                "key": "KP_Multiply",
                "modmask": 0,
                "description": "OpenWhisper: record_toggle",
                "release": False,
            }
        ],
    )
    evaluate = Mock()
    monkeypatch.setattr(hyprland, "evaluate", evaluate)
    native._sync()
    assert evaluate.call_args_list[0].args[0] == 'hl.unbind("KP_Multiply")'
    assert evaluate.call_count == 3
    assert "release=true" in evaluate.call_args_list[2].args[0]
