"""Copying the selection: which apps get a copy, and waiting for the shortcut's keys."""

import sys
import types
from unittest.mock import Mock

import pytest

from services import app_styles, hyprland, synthetic_keys
from services.app_styles import AppStyle, Surface
from services.focus_context import AppIdentity


def _app(app_id, name="", window="", platform="windows"):
    return AppIdentity(app_id, name, window=window, platform=platform)


@pytest.mark.parametrize("identity", [
    _app("WindowsTerminal.exe", "Windows Terminal"),
    _app(r"C:\Windows\System32\conhost.exe", "Console Window Host"),
    _app("OpenConsole.exe"),
    _app("powershell.exe", "Windows PowerShell"),
    _app("pwsh.exe"),
    _app("cmd.exe", "Command Prompt"),
    _app("wezterm-gui.exe"),
    _app("alacritty"),
    _app("kitty", platform="hyprland"),
    _app("com.mitchellh.ghostty", platform="hyprland"),
    _app("foot", platform="hyprland"),
    _app("com.googlecode.iterm2", "iTerm2", platform="macos"),
    _app("com.apple.Terminal", "Terminal", platform="macos"),
    _app("unknown", "Unknown", window="CASCADIA_HOSTING_WINDOW_CLASS"),
])
def test_terminals_never_get_a_copy(identity):
    assert synthetic_keys.is_terminal(identity)
    assert not synthetic_keys.copies_line_without_selection(identity)


@pytest.mark.parametrize("identity", [
    _app("Code.exe", "Visual Studio Code"),
    _app("Cursor.exe", "Cursor"),
    _app("Windsurf.exe"),
    _app("devenv.exe", "Visual Studio"),
    _app("idea64.exe", "IntelliJ IDEA"),
    _app("pycharm64.exe", "PyCharm"),
    _app("rider64.exe"),
    _app("studio64.exe", "Android Studio"),
    _app("sublime_text.exe", "Sublime Text"),
    _app("zed", platform="linux"),
    _app("jetbrains-pycharm", platform="hyprland"),
    _app("com.jetbrains.goland", "GoLand", platform="macos"),
    _app("com.microsoft.VSCode", "Code", platform="macos"),
])
def test_editors_that_copy_the_line_are_known(identity):
    assert synthetic_keys.copies_line_without_selection(identity)
    assert not synthetic_keys.is_terminal(identity)


@pytest.mark.parametrize("identity", [
    None,
    _app("outlook.exe", "Outlook"),
    _app("chrome.exe", "Google Chrome"),
    _app("notepad.exe", "Notepad"),
])
def test_ordinary_apps_are_neither(identity):
    assert not synthetic_keys.is_terminal(identity)
    assert not synthetic_keys.copies_line_without_selection(identity)


def test_powershell_ise_is_an_editor_not_a_terminal():
    # Its console pane runs commands, but the window is a script editor, so a
    # copy is safe to send there.
    assert not synthetic_keys.is_terminal(_app("powershell_ise.exe", "Windows PowerShell ISE"))


def test_the_app_catalogue_surface_counts_too(monkeypatch):
    surfaces = {"warpish": Surface.TERMINAL, "ide": Surface.CODE}
    monkeypatch.setattr(
        app_styles, "style_for",
        lambda snapshot, settings: AppStyle("other", "formal", surface=surfaces.get(
            snapshot.identity.app_id, Surface.TEXT)),
    )
    assert synthetic_keys.is_terminal(_app("warpish"))
    assert synthetic_keys.copies_line_without_selection(_app("ide"))
    assert not synthetic_keys.is_terminal(_app("ide"))


def test_a_broken_catalogue_or_identity_never_raises(monkeypatch):
    def broken(*_args):
        raise RuntimeError("catalogue failed")

    monkeypatch.setattr(app_styles, "style_for", broken)
    weird = types.SimpleNamespace(app_id=None, name=42, window=object())
    assert synthetic_keys.is_terminal(weird) is False
    assert synthetic_keys.copies_line_without_selection(weird) is False
    assert synthetic_keys.is_terminal(_app("outlook.exe")) is False


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(synthetic_keys.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(synthetic_keys.time, "sleep", clock.sleep)
    return clock


def test_waits_until_the_modifiers_come_up(monkeypatch, clock):
    held = iter([True, True, True, False])
    monkeypatch.setattr(synthetic_keys, "_modifier_probe", lambda: lambda: next(held))

    assert synthetic_keys.wait_for_modifiers_released(0.8) is True
    assert clock.sleeps == [synthetic_keys._POLL_S] * 3


def test_gives_up_at_the_timeout(monkeypatch, clock):
    monkeypatch.setattr(synthetic_keys, "_modifier_probe", lambda: lambda: True)

    assert synthetic_keys.wait_for_modifiers_released(0.05) is False
    assert 0.05 <= sum(clock.sleeps) <= 0.05 + synthetic_keys._POLL_S * 1.5


def test_unreadable_key_state_waits_briefly_and_counts_as_released(monkeypatch, clock):
    monkeypatch.setattr(synthetic_keys, "_modifier_probe", lambda: None)
    assert synthetic_keys.wait_for_modifiers_released(0.8) is True
    assert clock.sleeps == [synthetic_keys._BLIND_WAIT_S]

    def broken():
        raise OSError("hook gone")

    monkeypatch.setattr(synthetic_keys, "_modifier_probe", lambda: broken)
    assert synthetic_keys.wait_for_modifiers_released(0.8) is True


def test_windows_reads_every_modifier_from_the_keyboard_hook(monkeypatch):
    pressed = set()
    keyboard = types.SimpleNamespace(is_pressed=lambda name: name in pressed, send=Mock())
    monkeypatch.setitem(sys.modules, "keyboard", keyboard)

    assert synthetic_keys._windows_modifiers_held() is False
    for name in ("ctrl", "alt", "shift", "windows"):
        pressed.clear()
        pressed.add(name)
        assert synthetic_keys._windows_modifiers_held() is True


def test_windows_copy_is_ctrl_c(monkeypatch):
    keyboard = types.SimpleNamespace(send=Mock())
    monkeypatch.setitem(sys.modules, "keyboard", keyboard)
    monkeypatch.setattr(synthetic_keys.sys, "platform", "win32")

    synthetic_keys.send_copy()

    keyboard.send.assert_called_once_with("ctrl+c")


def test_wayland_copies_through_hyprland_or_says_it_cannot(monkeypatch):
    from services import desktop_session

    monkeypatch.setattr(synthetic_keys.sys, "platform", "linux")
    monkeypatch.setattr(desktop_session, "is_wayland_session", lambda: True)
    send_copy = Mock()
    monkeypatch.setattr(hyprland, "send_copy", send_copy)
    monkeypatch.setattr(hyprland, "available", lambda: True)
    synthetic_keys.send_copy()
    send_copy.assert_called_once_with()

    monkeypatch.setattr(hyprland, "available", lambda: False)
    with pytest.raises(RuntimeError, match="Wayland"):
        synthetic_keys.send_copy()


@pytest.mark.parametrize("app, mods", [("org.mozilla.firefox", "CTRL"), ("foot", "CTRL SHIFT")])
def test_hyprland_copy_targets_the_focused_window(monkeypatch, app, mods):
    monkeypatch.setattr(hyprland, "query", lambda _: {"address": "0x123abc", "class": app})
    evaluate = Mock()
    monkeypatch.setattr(hyprland, "evaluate", evaluate)

    hyprland.send_copy()

    code = evaluate.call_args.args[0]
    assert f'mods="{mods}"' in code
    assert 'key="c"' in code
    assert 'window="address:0x123abc"' in code


def test_hyprland_paste_still_sends_v(monkeypatch):
    monkeypatch.setattr(hyprland, "query", lambda _: {"address": "0x1", "class": "kitty"})
    evaluate = Mock()
    monkeypatch.setattr(hyprland, "evaluate", evaluate)

    hyprland.send_paste()

    assert evaluate.call_args.args[0] == (
        'hl.dispatch(hl.dsp.send_shortcut({mods="CTRL SHIFT", key="v", window="address:0x1"}))'
    )


@pytest.mark.parametrize("client, message", [
    ({"address": "0x1", "class": "openwhisper"}, "text to change"),
    ({"address": "", "class": "kitty"}, "No focused window"),
])
def test_hyprland_copy_refuses_our_own_window_and_no_window(monkeypatch, client, message):
    monkeypatch.setattr(hyprland, "query", lambda _: client)
    evaluate = Mock()
    monkeypatch.setattr(hyprland, "evaluate", evaluate)

    with pytest.raises(RuntimeError, match=message):
        hyprland.send_copy()
    evaluate.assert_not_called()
