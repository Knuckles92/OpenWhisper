"""Hotkey imports and startup degradation without an X display."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_without_display(source: str, *, wayland: bool) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env.pop("PYNPUT_BACKEND", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    if wayland:
        env["XDG_SESSION_TYPE"] = "wayland"
        env["WAYLAND_DISPLAY"] = "wayland-0"
    else:
        env.pop("XDG_SESSION_TYPE", None)
        env.pop("WAYLAND_DISPLAY", None)

    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pynput selects X11")
@pytest.mark.parametrize("wayland", [False, True], ids=["headless", "wayland"])
def test_ui_and_runtime_import_without_display_or_pynput_backend(wayland):
    result = _run_without_display(
        """
import sys

import services.hotkey_manager as hotkeys
import services.runtime.hotkeys
import ui_qt.main_window

assert hotkeys.format_hotkey_display("ctrl+alt+r")
assert "pynput" not in sys.modules
print("imports-ok")
""",
        wayland=wayland,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "imports-ok" in result.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pynput selects X11")
def test_wayland_hotkey_manager_degrades_to_local_controls():
    result = _run_without_display(
        """
from services.hotkey_manager import HotkeyManager

manager = HotkeyManager({"record_toggle": "ctrl+alt+r"})
assert manager.backend_available is False
assert manager.backend_name == "unavailable"
assert manager.backend_error
assert manager._listener is None

# The native listener is unavailable, but the dependency-free matcher remains
# usable by the focused-window Qt fallback.
assert manager.handle_hotkey_press(
    frozenset({"ctrl", "alt"}), "r", source="qt"
)
manager.rehook()
assert manager.backend_available is False
manager.cleanup()
print("degraded-ok")
""",
        wayland=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "degraded-ok" in result.stdout
