"""Screen capture on Hyprland through grim.

Wayland gives clients no way to read the screen, so Qt's grabWindow comes
back empty there. grim captures one output through the screencopy protocol
Hyprland implements and writes a PNG to stdout; it is quick enough to run on
the Qt thread under a short timeout. hyprctl tells us which output that is,
because Wayland also hides the pointer's global position from clients.
"""

from __future__ import annotations

import logging
import subprocess

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QGuiApplication, QImage, QScreen

from services import hyprland

logger = logging.getLogger(__name__)

GRIM_TIMEOUT_S = 2.0


def grim_command(output: str) -> list[str]:
    """grim's command line: one output (all when ``output`` is ""), PNG to stdout."""
    return ["grim", *(("-o", output) if output else ()), "-"]


def _run(command: list[str]) -> subprocess.CompletedProcess:
    """Run grim. The seam tests replace; never a shell."""
    return subprocess.run(
        command, capture_output=True, timeout=GRIM_TIMEOUT_S, check=False
    )


def grab(screen: QScreen) -> QImage | None:
    """``screen``'s pixels from grim, in device pixels, or None when grim fails."""
    result = _run(grim_command(output_name(screen)))
    if result.returncode != 0 or not result.stdout:
        detail = (result.stderr or b"").decode(errors="replace").strip()[:200]
        logger.warning("grim failed with exit code %s: %s", result.returncode, detail)
        return None
    image = QImage.fromData(result.stdout, "PNG")
    return None if image.isNull() else image


def _monitors() -> list[dict]:
    try:
        monitors = hyprland.query("monitors")
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.warning("Reading Hyprland's monitors failed", exc_info=True)
        return []
    if not isinstance(monitors, list):
        return []
    return [monitor for monitor in monitors if isinstance(monitor, dict)]


def _at(monitor: dict, point: QPoint) -> bool:
    return (monitor.get("x"), monitor.get("y")) == (point.x(), point.y())


def output_name(screen: QScreen) -> str:
    """The Hyprland output (``DP-1``) that shows ``screen``.

    Qt's screen name is the output name on native Wayland; under XWayland it
    is not, and the screen's position finds the monitor instead. Failing
    both, the focused monitor: Snap captures the screen under the pointer.
    """
    monitors = _monitors()
    names = [monitor.get("name", "") for monitor in monitors]
    if screen.name() in names:
        return screen.name()
    for monitor in monitors:
        if _at(monitor, screen.geometry().topLeft()):
            return monitor.get("name", "")
    focused = next((m.get("name", "") for m in monitors if m.get("focused")), "")
    return focused or screen.name()


def focused_screen() -> QScreen | None:
    """The screen of Hyprland's focused monitor (it follows the pointer), or None."""
    monitor = next((m for m in _monitors() if m.get("focused")), None)
    if monitor is None:
        return None
    for screen in QGuiApplication.screens():
        if screen.name() == monitor.get("name") or _at(
            monitor, screen.geometry().topLeft()
        ):
            return screen
    return None
