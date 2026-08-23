"""Meeting Mode platform support policy.

Meeting Mode captures two channels: a microphone, which PortAudio provides
everywhere, and system audio, which has no portable backend. A platform is
supported when it has a system-audio path -- WASAPI loopback on Windows,
ScreenCaptureKit on macOS 13+. Linux has neither, so it can still open the UI
and, after an explicit acknowledgement, try a microphone-only session.

macOS support carries a condition Windows does not: system audio is gated by
the Screen Recording TCC grant. The platform is still "supported" without it,
the same way Windows is supported on a machine with no loopback device -- the
grant is a runtime prerequisite for the ``loopback`` channel, checked
separately so the UI can walk the user to System Settings rather than silently
recording a mic-only meeting.
"""
from __future__ import annotations

import logging
import platform as platform_module
import sys
from typing import Optional

logger = logging.getLogger(__name__)

#: ScreenCaptureKit gained system-audio capture in macOS 13 (Ventura).
MIN_MACOS_MAJOR = 13

#: Deep link to the pane holding the Screen Recording grant. Named
#: "Screen & System Audio Recording" since macOS 14.
SCREEN_RECORDING_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_ScreenCapture"
)


def _macos_major(release: Optional[str] = None) -> int:
    """Major macOS version, or 0 when it cannot be determined."""
    release = release if release is not None else platform_module.mac_ver()[0]
    try:
        return int(release.split(".")[0]) if release else 0
    except ValueError:
        return 0


def meeting_mode_supported(platform: Optional[str] = None) -> bool:
    """True when this OS has a first-class Meeting Mode capture path."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return True
    if platform == "darwin":
        major = _macos_major()
        # An undetectable version is treated as new enough: ScreenCaptureKit
        # will rule itself out at capture time, and refusing to open the tab
        # is the worse failure.
        return major == 0 or major >= MIN_MACOS_MAJOR
    return False


def meeting_unsupported_os_name(platform: Optional[str] = None) -> str:
    """User-facing OS name for unsupported-platform copy."""
    platform = platform or sys.platform
    if platform == "darwin":
        return "macOS"
    if platform.startswith("linux"):
        return "Linux"
    return "this platform"


def system_audio_permission_required(platform: Optional[str] = None) -> bool:
    """True when this OS gates system-audio capture behind a user grant."""
    return (platform or sys.platform) == "darwin"


def system_audio_permission_granted() -> bool:
    """Whether system audio can be captured without prompting the user.

    Uses the preflight API, which reports the current grant without raising
    a dialog, so the capture watchdog and start-time checks can call it
    freely. Returns True off macOS, where no grant applies.
    """
    if not system_audio_permission_required():
        return True
    try:
        from Quartz import CGPreflightScreenCaptureAccess
    except Exception as exc:
        logger.warning("Could not preflight the Screen Recording grant: %s", exc)
        return False
    try:
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        logger.exception("Screen Recording preflight failed")
        return False


def request_system_audio_permission() -> bool:
    """Ask macOS for the Screen Recording grant, prompting once if needed.

    macOS shows its dialog only the first time a given binary asks; after a
    denial the request returns False immediately and the user has to change it
    in System Settings, which is why callers pair this with a link there.

    Returns:
        True when the grant is held afterwards.
    """
    if not system_audio_permission_required():
        return True
    try:
        from Quartz import CGRequestScreenCaptureAccess
    except Exception as exc:
        logger.warning("Could not request the Screen Recording grant: %s", exc)
        return False
    try:
        return bool(CGRequestScreenCaptureAccess())
    except Exception:
        logger.exception("Screen Recording request failed")
        return False
