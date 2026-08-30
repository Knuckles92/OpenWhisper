"""Meeting Mode platform support policy.

Meeting Mode captures two channels: a microphone, which PortAudio provides
everywhere, and system audio, which has a first-class path on Windows
(WASAPI/soundcard) and macOS 13+ (ScreenCaptureKit). Linux x86_64/aarch64 has a
complete PulseAudio / PipeWire-Pulse implementation in-tree, but public
``meeting_mode_supported`` promotion stays gated until the manual hardware
release matrix is attested.

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

_LINUX_X86_64_ALIASES = frozenset({"x86_64", "amd64", "x64"})
_LINUX_AARCH64_ALIASES = frozenset({"aarch64", "arm64"})


def normalize_linux_machine(machine: Optional[str] = None) -> Optional[str]:
    """Return ``linux_x86_64`` / ``linux_aarch64``, or None when unsupported."""
    raw = (machine if machine is not None else platform_module.machine()).strip().lower()
    if raw in _LINUX_X86_64_ALIASES:
        return "linux_x86_64"
    if raw in _LINUX_AARCH64_ALIASES:
        return "linux_aarch64"
    return None


def _macos_major(release: Optional[str] = None) -> int:
    """Major macOS version, or 0 when it cannot be determined."""
    release = release if release is not None else platform_module.mac_ver()[0]
    try:
        return int(release.split(".")[0]) if release else 0
    except ValueError:
        return 0


def linux_meeting_implementation_ready(machine: Optional[str] = None) -> bool:
    """True when the Linux Meeting Mode implementation covers this arch.

    Distinct from public ``meeting_mode_supported``: the code path is complete
    for x86_64/aarch64, but promotion remains gated on hardware attestation.
    """
    return normalize_linux_machine(machine) is not None


def meeting_mode_supported(
    platform: Optional[str] = None,
    machine: Optional[str] = None,
) -> bool:
    """True when this OS is publicly promoted as a Meeting Mode platform."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return True
    if platform == "darwin":
        major = _macos_major()
        # Unknown macOS versions fail closed: ScreenCaptureKit audio needs a
        # known Ventura+ baseline rather than an optimistic guess.
        return major >= MIN_MACOS_MAJOR
    # Linux implementation exists, but public support stays gated until the
    # manual x86_64/aarch64 hardware release matrix is attested.
    if platform.startswith("linux"):
        return False
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
