"""Why other computers might not reach this one while it shares its engine.

What this computer can see for itself, each with a plain fix:

* Windows treats the network as Public. The rule Windows offers when sharing
  starts usually covers private networks only, so computers on a Public one
  are turned away; the most common "it can't find my PC" on Windows.
* A VPN is on. Many block the local network unless told not to (Proton VPN's
  "Allow LAN connections", for one).
* Discovery couldn't listen, so other computers won't find this one by
  searching; they can still enter its address.

What it can't see (a router keeping Wi-Fi and wired devices apart, the other
computer's own firewall) the client says instead, from what happened when it
tried to connect.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

PUBLIC = "public"
PRIVATE = "private"
DOMAIN = "domain"

#: Get-NetConnectionProfile's NetworkCategory, as a number (PowerShell 5) or a name.
_CATEGORIES = {0: PUBLIC, 1: PRIVATE, 2: DOMAIN,
               "public": PUBLIC, "private": PRIVATE, "domainauthenticated": DOMAIN}


@dataclass(frozen=True)
class NetworkProfile:
    """A connected Windows network: its name, adapter, and category."""

    name: str
    interface: str
    category: str


@dataclass(frozen=True)
class Note:
    """One reason others may not reach this computer, and what to do about it."""

    kind: str
    message: str
    #: A button's label, and what it opens (a URL or ms-settings: link).
    action: str = ""
    url: str = ""


def parse_profiles(text: str) -> List[NetworkProfile]:
    try:
        data = json.loads(text) if text and text.strip() else []
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    profiles = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("NetworkCategory")
        category = _CATEGORIES.get(raw.lower() if isinstance(raw, str) else raw)
        if category is None:
            continue
        profiles.append(NetworkProfile(
            name=str(entry.get("Name") or ""),
            interface=str(entry.get("InterfaceAlias") or ""),
            category=category,
        ))
    return profiles


def windows_network_profiles(timeout: float = 8.0) -> List[NetworkProfile]:
    """The connected networks and whether Windows treats each as public. Blocks ~0.3 s."""
    if sys.platform != "win32":
        return []
    command = ("Get-NetConnectionProfile | Select-Object Name,InterfaceAlias,NetworkCategory "
               "| ConvertTo-Json -Compress")
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("Could not read the Windows network profiles", exc_info=True)
        return []
    if completed.returncode != 0:
        return []
    return parse_profiles(completed.stdout.decode("utf-8", "replace"))


def _settings_page(interface: str) -> str:
    name = interface.lower()
    if "wi-fi" in name or "wifi" in name or "wireless" in name or "wlan" in name:
        return "ms-settings:network-wifi"
    if "ethernet" in name:
        return "ms-settings:network-ethernet"
    return "ms-settings:network-status"


def host_notes(lan, addresses: Sequence, profiles: Optional[Sequence[NetworkProfile]],
               discovery_error: str = "") -> List[Note]:
    """What may keep computers on this network out, most likely first.

    ``lan`` is ``lan_address.best_lan_address()``, ``addresses`` all of
    ``local_ipv4_addresses()``, ``profiles`` the Windows network profiles
    (None while they haven't been read), ``discovery_error`` why discovery
    isn't answering.
    """
    from services.lan_address import LAN, VPN

    notes = []
    if lan is not None and lan.kind == LAN and profiles:
        profile = next((p for p in profiles if p.interface == lan.interface), None)
        if profile is not None and profile.category == PUBLIC:
            network = f'"{profile.name}"' if profile.name else "this network"
            notes.append(Note(
                "public_network",
                f"Windows treats {network} as a public network, which usually keeps other "
                "computers from connecting here. If they can't, make it a private network.",
                action="Open network settings",
                url=_settings_page(profile.interface),
            ))
    if lan is not None and lan.kind == LAN and any(a.kind == VPN for a in addresses):
        notes.append(Note(
            "vpn",
            "A VPN is on. If other computers can't connect, allow local network (LAN) "
            "access in its settings.",
        ))
    if discovery_error:
        notes.append(Note(
            "discovery",
            f"Other computers can't find this one by searching ({discovery_error}). They can "
            "still enter its address and pairing code.",
        ))
    return notes
