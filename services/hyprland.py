"""Hyprland IPC helpers. Commands are bounded and never invoke a shell."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess


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


def send_paste() -> None:
    """Ask the compositor to paste into the currently focused application."""
    client = query("activewindow")
    address = client.get("address", "")
    if not re.fullmatch(r"0x[0-9a-fA-F]+", address):
        raise RuntimeError("No focused window to paste into")
    if client.get("class", "").lower() in ("openwhisper", "openwhisper-ui-qa"):
        raise RuntimeError(
            "Focus the destination application before stopping dictation"
        )
    # Terminals use a different paste shortcut. This mirrors their conventional
    # clipboard action, while editors/browsers use Ctrl+V.
    terminal = client.get("class", "").lower() in (
        "foot",
        "kitty",
        "alacritty",
        "com.mitchellh.ghostty",
        "org.wezfurlong.wezterm",
    )
    mods = "CTRL SHIFT" if terminal else "CTRL"
    evaluate(
        "hl.dispatch(hl.dsp.send_shortcut({mods="
        + json.dumps(mods)
        + ', key="v", window='
        + json.dumps("address:" + address)
        + "}))"
    )
