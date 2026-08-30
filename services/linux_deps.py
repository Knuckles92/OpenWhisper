"""Linux shared-library preflight for OpenWhisper.

Stdlib-only so it can run before PyQt or sounddevice is imported. Missing
microphone hardware is out of scope; this only checks documented SONAMEs.

Meeting Mode additionally needs PulseAudio client libraries. Those stay in a
separate non-fatal collection so a missing libpulse cannot block Quick Record
or the Meeting Mode remediation dialog.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, TextIO, Tuple

REQUIRED_LIBRARIES: Tuple[Tuple[str, Dict[str, str]], ...] = (
    ("libEGL.so.1", {
        "apt": "libegl1",
        "dnf": "mesa-libEGL",
        "pacman": "libgl",
    }),
    ("libGL.so.1", {
        "apt": "libgl1",
        "dnf": "libglvnd-glx",
        "pacman": "libgl",
    }),
    ("libxcb-cursor.so.0", {
        "apt": "libxcb-cursor0",
        "dnf": "xcb-util-cursor",
        "pacman": "xcb-util-cursor",
    }),
    ("libxkbcommon-x11.so.0", {
        "apt": "libxkbcommon-x11-0",
        "dnf": "libxkbcommon-x11",
        "pacman": "libxkbcommon",
    }),
    ("libxcb-icccm.so.4", {
        "apt": "libxcb-icccm4",
        "dnf": "xcb-util-wm",
        "pacman": "xcb-util-wm",
    }),
    ("libxcb-keysyms.so.1", {
        "apt": "libxcb-keysyms1",
        "dnf": "xcb-util-keysyms",
        "pacman": "xcb-util-keysyms",
    }),
    ("libxcb-xkb.so.1", {
        "apt": "libxcb-xkb1",
        "dnf": "libxcb",
        "pacman": "libxcb",
    }),
    ("libxcb-shape.so.0", {
        "apt": "libxcb-shape0",
        "dnf": "libxcb",
        "pacman": "libxcb",
    }),
    ("libportaudio.so.2", {
        "apt": "libportaudio2",
        "dnf": "portaudio",
        "pacman": "portaudio",
    }),
)

# Meeting Mode only. Missing entries must never fail app bootstrap.
MEETING_AUDIO_LIBRARIES: Tuple[Tuple[str, Dict[str, str]], ...] = (
    ("libpulse.so.0", {
        "apt": "libpulse0",
        "dnf": "pulseaudio-libs",
        "pacman": "libpulse",
    }),
)

LINUX_SYSTEM_AUDIO_GUIDE = "docs/linux-system-audio.md"


@dataclass(frozen=True)
class MeetingAudioRemediation:
    """Offline guidance for one Linux Meeting Mode capture failure."""

    reason: str
    package_family: str
    title: str
    explanation: str
    commands: Tuple[str, ...]
    verification: Tuple[str, ...] = ()
    restart_note: str = ""
    rollback_note: str = ""


def detect_package_family(
    os_release: str = "",
    *,
    fallback: str = "apt",
) -> str:
    """Return apt, dnf, pacman, or ``fallback`` from os-release contents."""
    text = os_release
    if not text:
        try:
            with open("/etc/os-release", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return fallback

    identity = ""
    for line in text.splitlines():
        if line.startswith("ID=") or line.startswith("ID_LIKE="):
            identity += " " + line.split("=", 1)[1].strip().strip('"')
    identity = identity.lower()
    if any(token in identity for token in ("fedora", "rhel", "centos", "rocky", "almalinux")):
        return "dnf"
    if any(token in identity for token in ("arch", "manjaro", "endeavouros")):
        return "pacman"
    if any(token in identity for token in ("debian", "ubuntu", "linuxmint", "pop", "elementary")):
        return "apt"
    return fallback


def probe_library(soname: str) -> bool:
    """Return True when ``soname`` can be loaded."""
    try:
        ctypes.CDLL(soname)
        return True
    except OSError:
        return False


def missing_libraries(
    required: Sequence[Tuple[str, Dict[str, str]]] = REQUIRED_LIBRARIES,
) -> List[Tuple[str, Dict[str, str]]]:
    """Return the required libraries that cannot be loaded."""
    return [(soname, packages) for soname, packages in required if not probe_library(soname)]


def install_command(
    missing: Sequence[Tuple[str, Dict[str, str]]],
    family: str = "apt",
) -> str:
    """Return the package-manager command that installs the missing libraries."""
    packages = [packages[family] for _, packages in missing if family in packages]
    if not packages:
        return ""
    if family == "dnf":
        return "sudo dnf install -y " + " ".join(packages)
    if family == "pacman":
        return "sudo pacman -S --needed " + " ".join(packages)
    if family == "apt":
        return "sudo apt install -y " + " ".join(packages)
    return ""


def _family_commands(family: str, packages: Sequence[str]) -> Tuple[str, ...]:
    packages = [pkg for pkg in packages if pkg]
    if not packages:
        return ()
    if family == "dnf":
        return (f"sudo dnf install -y {' '.join(packages)}",)
    if family == "pacman":
        return (f"sudo pacman -S --needed {' '.join(packages)}",)
    if family == "apt":
        return (f"sudo apt install -y {' '.join(packages)}",)
    return ()


def _service_commands(family: str) -> Tuple[str, ...]:
    # Advice only — never executed by the app.
    return (
        "systemctl --user enable --now pipewire pipewire-pulse wireplumber",
        "systemctl --user --type=service | grep -E 'pipewire|pulse'",
    )


def meeting_audio_remediation(
    reason: str,
    package_family: str = "unknown",
    server_kind: str = "unknown",
) -> MeetingAudioRemediation:
    """Return structured remediation for a Linux Meeting Mode capture failure.

    ``server_kind`` gates stack-changing advice. Native PulseAudio users with
    transient sink/monitor/open failures must never be told to install or
    enable a competing PipeWire stack.
    """
    family = package_family if package_family in {"apt", "dnf", "pacman"} else "unknown"
    stack = (server_kind or "unknown").strip().lower()

    titles = {
        "soundcard_missing": "SoundCard is not installed",
        "libpulse_missing": "PulseAudio client library is missing",
        "audio_server_unavailable": "No Pulse-compatible audio server is running",
        "pipewire_pulse_missing": "PipeWire is running without PulseAudio compatibility",
        "default_sink_missing": "No default audio output was found",
        "pactl_missing": "Optional pactl fallback is not installed",
        "monitor_source_missing": "System-audio monitor source is unavailable",
        "monitor_open_failed": "System-audio monitor could not be opened",
        "unsupported_architecture": "This Linux architecture is not supported",
        "unknown_failure": "System audio could not be prepared",
    }
    title = titles.get(reason, titles["unknown_failure"])

    if reason == "soundcard_missing":
        commands = (
            "python -m pip install 'soundcard>=0.4.3'",
        )
        explanation = (
            "Meeting Mode uses the SoundCard package to capture the default "
            "output mix on Linux. Install it into the same environment that "
            "runs OpenWhisper, then retry detection."
        )
        return MeetingAudioRemediation(
            reason=reason,
            package_family=family,
            title=title,
            explanation=explanation,
            commands=commands,
            verification=("python -c \"import soundcard; print(soundcard.__version__)\"",),
        )

    if reason == "libpulse_missing":
        packages = {
            "apt": ("libpulse0",),
            "dnf": ("pulseaudio-libs",),
            "pacman": ("libpulse",),
        }.get(family, ())
        commands = _family_commands(family, packages)
        explanation = (
            "OpenWhisper needs the PulseAudio client library to talk to "
            "PulseAudio or PipeWire's Pulse compatibility layer."
        )
        if not commands:
            explanation += (
                " Install the PulseAudio client library for your distribution, "
                "then retry detection."
            )
        return MeetingAudioRemediation(
            reason=reason,
            package_family=family,
            title=title,
            explanation=explanation,
            commands=commands,
            verification=("python -c \"import ctypes; ctypes.CDLL('libpulse.so.0')\"",),
        )

    if reason == "pipewire_pulse_missing":
        packages = {
            "apt": ("pipewire-pulse", "wireplumber"),
            "dnf": ("pipewire-pulseaudio", "wireplumber"),
            "pacman": ("pipewire-pulse", "wireplumber"),
        }.get(family, ())
        commands = _family_commands(family, packages) + _service_commands(family)
        explanation = (
            "PipeWire is active, but the PulseAudio compatibility service is "
            "not. Meeting Mode captures system audio through that compatibility "
            "layer."
        )
        if family == "unknown":
            explanation += (
                " Install pipewire-pulse and wireplumber for your distribution, "
                "enable the user services, then sign out and back in if needed."
            )
            commands = ()
        return MeetingAudioRemediation(
            reason=reason,
            package_family=family,
            title=title,
            explanation=explanation,
            commands=commands,
            verification=(
                "pactl info | grep -i 'Server Name'",
                "systemctl --user is-active pipewire-pulse",
            ),
            restart_note="Sign out and back in if the new services do not appear immediately.",
            rollback_note="You can disable pipewire-pulse later with systemctl --user disable --now pipewire-pulse.",
        )

    if reason == "audio_server_unavailable":
        # Diagnostic-only: never auto-start a competing stack. A paste of the
        # listed commands must not switch Pulse users onto PipeWire (or the
        # reverse) via ``cmd_a || cmd_b`` fallbacks.
        commands = (
            "systemctl --user status pulseaudio pipewire pipewire-pulse wireplumber",
            "systemctl --user is-active pulseaudio",
            "systemctl --user is-active pipewire-pulse",
            "pactl info",
            "pactl list short sinks",
            "pactl list short sources",
        )
        explanation = (
            "No Pulse-compatible audio server answered. Desktop Linux needs "
            "a running native PulseAudio session or PipeWire with "
            "pipewire-pulse before Meeting Mode can capture system audio. "
            "Identify which stack you already install, restart only that "
            "stack, then retry detection. Do not chain a Pulse start into a "
            "PipeWire start with '||'."
        )
        if family == "unknown":
            explanation += (
                " If no Pulse-compatible server is installed at all, see the "
                "setup guide for distribution-specific package names."
            )
        return MeetingAudioRemediation(
            reason=reason,
            package_family=family,
            title=title,
            explanation=explanation,
            commands=commands,
            verification=(
                "pactl info",
                "systemctl --user is-active pulseaudio",
                "systemctl --user is-active pipewire-pulse",
            ),
            restart_note=(
                "After you identify the installed stack, restart only that one: "
                "`systemctl --user restart pulseaudio` or "
                "`systemctl --user restart pipewire pipewire-pulse wireplumber`. "
                "Choose one; do not paste both as a single '||' command."
            ),
            rollback_note=(
                "Do not replace a working native Pulse install with PipeWire "
                "only to recover a temporary outage."
            ),
        )

    if reason == "pactl_missing":
        packages = {
            "apt": ("pulseaudio-utils",),
            "dnf": ("pulseaudio-utils",),
            "pacman": ("libpulse",),
        }.get(family, ())
        commands = _family_commands(family, packages)
        explanation = (
            "SoundCard could not directly prove which nonstandard loopback "
            "belongs to the default output. Install pactl so OpenWhisper can "
            "verify the exact PulseAudio or PipeWire-Pulse monitor association. "
            "System-audio capture remains optional."
        )
        if not commands:
            explanation += (
                " Install the package that provides pactl for your distribution, "
                "then retry detection."
            )
        return MeetingAudioRemediation(
            reason=reason,
            package_family=family,
            title=title,
            explanation=explanation,
            commands=commands,
            verification=(
                "pactl info",
                "pactl list short sinks",
                "pactl list short sources",
            ),
        )

    if reason in {
        "default_sink_missing",
        "monitor_source_missing",
        "monitor_open_failed",
    }:
        explanation = {
            "default_sink_missing": (
                "OpenWhisper found no default speaker/output sink. Choose a "
                "default output in your desktop sound settings, play a short "
                "sound to wake the device, then retry detection."
            ),
            "monitor_source_missing": (
                "The default output has no usable monitor source. Confirm "
                "PulseAudio or PipeWire-Pulse is running and that the default "
                "output is not a virtual device without a monitor."
            ),
            "monitor_open_failed": (
                "A monitor source was found but could not be opened. Another "
                "app may be holding exclusive access, or the audio server may "
                "need a restart."
            ),
        }[reason]
        commands = (
            "pactl info",
            "pactl list short sinks",
            "pactl list short sources",
        )
        # Never push a competing PipeWire stack onto a live PulseAudio session
        # for transient sink/monitor problems.
        if stack in {"", "unknown", "unavailable"} and family != "unknown":
            # Unknown stack: diagnostic commands only; no stack swap.
            pass
        elif stack == "pipewire-pulse" and family != "unknown":
            commands = commands + (
                "systemctl --user is-active pipewire pipewire-pulse wireplumber",
            )
        elif stack == "pulse":
            commands = commands + (
                "systemctl --user --type=service | grep -E 'pulse|pipewire' || true",
            )
        return MeetingAudioRemediation(
            reason=reason,
            package_family=family,
            title=title,
            explanation=explanation,
            commands=commands,
            verification=commands[:3],
            restart_note=(
                "If the monitor appears only after a service restart, sign out "
                "and back in once. Do not switch audio stacks for this error "
                "unless your desktop already intended to use PipeWire."
            ),
        )

    if reason == "unsupported_architecture":
        return MeetingAudioRemediation(
            reason=reason,
            package_family=family,
            title=title,
            explanation=(
                "Meeting Mode supports Linux on x86_64/amd64 and aarch64/arm64 "
                "only. Other architectures remain unsupported."
            ),
            commands=(),
        )

    return MeetingAudioRemediation(
        reason=reason or "unknown_failure",
        package_family=family,
        title=title,
        explanation=(
            "OpenWhisper could not prepare system-audio capture. Confirm a "
            "Pulse-compatible desktop audio session is running, install any "
            "missing client libraries, then retry detection. You can still "
            "continue with microphone only for this meeting."
        ),
        commands=(),
        verification=("pactl info",),
    )


def check_linux_dependencies(stream: TextIO | None = None) -> int:
    """Print an install command when required Linux libraries are missing.

    Args:
        stream: Destination for the message. Defaults to stderr.

    Returns:
        ``0`` when the platform is not Linux or every library is present,
        ``1`` when one or more required libraries are missing.
    """
    if not sys.platform.startswith("linux"):
        return 0

    missing = missing_libraries()
    if not missing:
        return 0

    out = stream if stream is not None else sys.stderr
    family = detect_package_family()
    print("OpenWhisper is missing required system libraries:", file=out)
    for soname, _packages in missing:
        print(f"  - {soname}", file=out)
    print("Install them with:", file=out)
    print(f"  {install_command(missing, family)}", file=out)
    print("Then re-run the command. Clipboard copy uses Qt and does not need xclip.", file=out)
    return 1
