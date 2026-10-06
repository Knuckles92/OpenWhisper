"""Hyprland's paste and copy keys, Command Mode and app awareness agree on what a terminal is."""
from unittest.mock import Mock

import pytest

from services import hyprland, synthetic_keys
from services.focus_context import _linux, catalog


def _identity(window_class):
    return _linux._identity(window_class, 0, "", "0x1", "hyprland")


def _hyprland_mods(monkeypatch, window_class, send=hyprland.send_paste):
    monkeypatch.setattr(hyprland, "query", lambda _: {"address": "0x1", "class": window_class})
    evaluate = Mock()
    monkeypatch.setattr(hyprland, "evaluate", evaluate)
    send()
    code = evaluate.call_args.args[0]
    return code.split('mods="', 1)[1].split('"', 1)[0]


# Tabby's Linux window class, and ids WezTerm, Warp and foot also go by.
@pytest.mark.parametrize("window_class", [
    "tabby", "Tabby", "wezterm-gui", "warp", "org.codeberg.dnkl.foot",
])
def test_every_check_knows_these_terminals(monkeypatch, window_class):
    identity = _identity(window_class)

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
    identity = _identity(window_class)
    terminal = catalog.is_terminal(identity)

    assert synthetic_keys.is_terminal(identity) is terminal
    assert _hyprland_mods(monkeypatch, window_class) == ("CTRL SHIFT" if terminal else "CTRL")
