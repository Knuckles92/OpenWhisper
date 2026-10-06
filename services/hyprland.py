"""Hyprland IPC helpers. Commands are bounded and never invoke a shell."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

_OWN_CLASSES = ("openwhisper", "openwhisper-ui-qa")


def _is_terminal(client: dict) -> bool:
    """Whether the window is a terminal, by the check Command Mode uses too.

    Terminals use Ctrl+Shift for the clipboard: Ctrl+C would interrupt the
    running program and Ctrl+V reaches it as a control character.
    """
    from services import synthetic_keys
    from services.focus_context import _linux

    return synthetic_keys.is_terminal(_linux._hyprland_identity(client))


def available() -> bool:
    return bool(
        os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl")
    )


def query(name: str):
    return json.loads(
        subprocess.check_output(["hyprctl", "-j", name], text=True, timeout=2)
    )


def evaluate(code: str) -> None:
    result = subprocess.run(
        ["hyprctl", "eval", code], capture_output=True, text=True, timeout=2, check=True
    )
    if result.stdout.strip() != "ok":
        raise RuntimeError(result.stdout.strip() or result.stderr.strip())


def _send_shortcut(key: str, *, no_window: str, own_window: str) -> None:
    """Send Ctrl+``key`` (Ctrl+Shift in terminals) to the focused window."""
    client = query("activewindow")
    address = client.get("address", "")
    if not re.fullmatch(r"0x[0-9a-fA-F]+", address):
        raise RuntimeError(no_window)
    window_class = client.get("class", "").lower()
    if window_class in _OWN_CLASSES:
        raise RuntimeError(own_window)
    mods = "CTRL SHIFT" if _is_terminal(client) else "CTRL"
    evaluate(
        "hl.dispatch(hl.dsp.send_shortcut({mods="
        + json.dumps(mods)
        + ", key="
        + json.dumps(key)
        + ", window="
        + json.dumps("address:" + address)
        + "}))"
    )


def send_paste() -> None:
    """Ask the compositor to paste into the currently focused application."""
    _send_shortcut(
        "v",
        no_window="No focused window to paste into",
        own_window="Focus the destination application before stopping dictation",
    )


def send_copy() -> None:
    """Ask the compositor to copy the focused application's selection."""
    _send_shortcut(
        "c",
        no_window="No focused window to copy from",
        own_window="Focus the app with the text to change first",
    )
