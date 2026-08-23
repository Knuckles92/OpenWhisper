"""Linux shared-library preflight for OpenWhisper.

Stdlib-only so it can run before PyQt or sounddevice is imported. Missing
microphone hardware is out of scope; this only checks documented SONAMEs.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Dict, List, Sequence, TextIO, Tuple

REQUIRED_LIBRARIES: Tuple[Tuple[str, Dict[str, str]], ...] = (
    ("libEGL.so.1", {
        "apt": "libegl1",
        "dnf": "mesa-libEGL",
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
    ("libportaudio.so.2", {
        "apt": "libportaudio2",
        "dnf": "portaudio",
        "pacman": "portaudio",
    }),
)


def detect_package_family(os_release: str = "") -> str:
    """Return apt, dnf, or pacman from os-release contents."""
    text = os_release
    if not text:
        try:
            with open("/etc/os-release", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return "apt"

    identity = ""
    for line in text.splitlines():
        if line.startswith("ID=") or line.startswith("ID_LIKE="):
            identity += " " + line.split("=", 1)[1].strip().strip('"')
    identity = identity.lower()
    if any(token in identity for token in ("fedora", "rhel", "centos", "rocky", "almalinux")):
        return "dnf"
    if any(token in identity for token in ("arch", "manjaro", "endeavouros")):
        return "pacman"
    return "apt"


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
    if family == "dnf":
        return "sudo dnf install -y " + " ".join(packages)
    if family == "pacman":
        return "sudo pacman -S --needed " + " ".join(packages)
    return "sudo apt install -y " + " ".join(packages)


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
