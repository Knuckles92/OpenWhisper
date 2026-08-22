"""Meeting Mode platform support policy.

Meeting Mode v1 is a Windows capture path (WASAPI loopback plus a
Windows-only ``soundcard`` fallback). Other operating systems can still
open the UI and, after an explicit acknowledgement, try a microphone-only
session — but that path is unsupported.
"""
from __future__ import annotations

import sys
from typing import Optional


def meeting_mode_supported(platform: Optional[str] = None) -> bool:
    """True when this OS has a first-class Meeting Mode capture path."""
    return (platform or sys.platform).startswith("win")


def meeting_unsupported_os_name(platform: Optional[str] = None) -> str:
    """User-facing OS name for unsupported-platform copy."""
    platform = platform or sys.platform
    if platform == "darwin":
        return "macOS"
    if platform.startswith("linux"):
        return "Linux"
    return "this platform"
