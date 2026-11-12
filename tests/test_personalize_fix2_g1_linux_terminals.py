"""Common Linux terminals are known by window class, so none gets a Ctrl+C.

In VTE, Qt and st terminals Ctrl+C interrupts the running program; copy and
paste there are Ctrl+Shift+C and Ctrl+Shift+V. The app catalogue is the one
list every check reads.
"""

from unittest.mock import Mock

import pytest

from config import config
from services import cleanup_prompts, hyprland, synthetic_keys
from services.focus_context import FocusSnapshot, _linux, catalog
from services.settings import SettingsKey
from tests.test_personalize_s3_command_runtime import _stop_and_read, h  # noqa: F401  (fixtures)

# (session, the class X11 WM_CLASS or Hyprland reports)
LINUX_TERMINALS = [
    ("x11", "Mate-terminal"),
    ("x11", "Lxterminal"),
    ("x11", "qterminal"),
    ("x11", "Ptyxis"),
    ("hyprland", "org.gnome.Ptyxis"),
    ("x11", "St"),
    ("hyprland", "st-256color"),
    ("x11", "Terminology"),
    ("x11", "Guake"),
    ("x11", "yakuake"),
    ("hyprland", "org.kde.yakuake"),
    ("x11", "Sakura"),
    ("x11", "cool-retro-term"),
    ("x11", "Gnome-terminal"),
    ("x11", "io.elementary.terminal"),
    ("x11", "Deepin-terminal"),
    ("hyprland", "com.raggesilver.BlackBox"),
    ("x11", "Tilda"),
    ("x11", "Hyper"),
    ("hyprland", "org.gnome.Terminal"),
    ("hyprland", "org.kde.konsole"),
]


def _identity(session, app_class):
    return _linux._identity(app_class, 0, "", "0x1", session)


@pytest.mark.parametrize("session, app_class", LINUX_TERMINALS)
def test_linux_terminals_are_known_by_window_class(session, app_class):
    identity = _identity(session, app_class)

    assert catalog.is_terminal(identity)
    assert synthetic_keys.is_terminal(identity)
    assert not synthetic_keys.copies_line_without_selection(identity)


@pytest.mark.parametrize("app_class", [
    "firefox", "org.gnome.TextEditor", "gedit", "libreoffice-writer", "obsidian",
])
def test_ordinary_linux_apps_are_not_terminals(app_class):
    assert not synthetic_keys.is_terminal(_identity("x11", app_class))


def test_spoken_lists_stay_on_one_line_in_a_newly_known_terminal():
    settings = {
        SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True,
        SettingsKey.TRANSCRIPT_CLEANUP_LEVEL: "medium",
    }
    snapshot = FocusSnapshot(_identity("x11", "Mate-terminal"))

    assert cleanup_prompts.inline_lists_block(settings, snapshot) == (
        config.TRANSCRIPT_CLEANUP_TERMINAL_LINES
    )


@pytest.mark.parametrize("session, app_class", [
    ("x11", "Mate-terminal"), ("hyprland", "org.gnome.Ptyxis"),
])
def test_command_mode_never_copies_from_a_newly_known_terminal(h, session, app_class):  # noqa: F811
    h.service.snapshot = FocusSnapshot(_identity(session, app_class), None)
    h.ui.clipboard_selection = "would have interrupted the build"

    assert _stop_and_read(h) == ""
    assert h.ui.captures == [] and h.released == []


def _hyprland_mods(monkeypatch, window_class, send=hyprland.send_paste):
    monkeypatch.setattr(hyprland, "query", lambda _: {"address": "0x1", "class": window_class})
    evaluate = Mock()
    monkeypatch.setattr(hyprland, "evaluate", evaluate)
    send()
    code = evaluate.call_args.args[0]
    return code.split('mods="', 1)[1].split('"', 1)[0]


@pytest.mark.parametrize("window_class, mods", [
    ("org.gnome.ptyxis", "CTRL SHIFT"),
    ("konsole", "CTRL SHIFT"),
    ("gnome-terminal", "CTRL SHIFT"),
    ("kitty", "CTRL SHIFT"),
    ("firefox", "CTRL"),
])
def test_hyprland_uses_ctrl_shift_in_every_known_terminal(monkeypatch, window_class, mods):
    assert _hyprland_mods(monkeypatch, window_class) == mods


# Hyprland's paste and copy keys, Command Mode and app awareness agree on what a terminal is.
# Tabby's Linux window class, and ids WezTerm, Warp and foot also go by.
@pytest.mark.parametrize("window_class", [
    "tabby", "Tabby", "wezterm-gui", "warp", "org.codeberg.dnkl.foot",
])
def test_every_check_knows_these_terminals(monkeypatch, window_class):
    identity = _identity("hyprland", window_class)

    assert catalog.is_terminal(identity)
    assert synthetic_keys.is_terminal(identity)
    assert _hyprland_mods(monkeypatch, window_class) == "CTRL SHIFT"
    assert _hyprland_mods(monkeypatch, window_class, hyprland.send_copy) == "CTRL SHIFT"


_CLASSES = sorted(
    {app_id for app in catalog._APPS for app_id in app.ids if not app_id.endswith(".exe")}
    | {"terminal", "iterm2", "mintty", "pwsh", "obsidian", "unknown-app"}
)


@pytest.mark.parametrize("window_class", _CLASSES)
def test_the_paste_keys_follow_the_same_terminal_check(monkeypatch, window_class):
    identity = _identity("hyprland", window_class)
    terminal = catalog.is_terminal(identity)

    assert synthetic_keys.is_terminal(identity) is terminal
    assert _hyprland_mods(monkeypatch, window_class) == ("CTRL SHIFT" if terminal else "CTRL")
