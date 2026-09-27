"""Tailscale, when this computer has it: find peers and identify connections.

Everything goes through the ``tailscale`` CLI's JSON output (``status --json``
and ``whois --json``). Both are read-only questions the local tailscaled
answers from what it already knows, they need no admin rights, and the CLI
behaves the same on Windows, macOS and Linux. Nothing here talks to
Tailscale's servers.

The host uses ``whois`` to learn which Tailscale user owns the computer on
the other end of a connection. Tailscale learns that from the WireGuard key
the packets arrived under, so it can't be claimed by the connecting side.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: Addresses Tailscale hands out: the CGNAT range and its ULA prefix.
_TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)
#: Operating systems OpenWhisper runs on, as ``status --json`` spells them.
DESKTOP_OSES = frozenset({"windows", "linux", "macos"})
#: What ``whois`` reports as the owner of a tagged (server) node.
_TAGGED_OWNER = "tagged-devices"

_STATES = {
    "Running": "running",
    "Stopped": "stopped",
    "NeedsLogin": "needs_login",
    "NeedsMachineAuth": "needs_login",
    "Starting": "starting",
    "NoState": "starting",
}


class TailscaleError(RuntimeError):
    """The CLI is missing, tailscaled isn't running, or its answer was unreadable."""


@dataclass(frozen=True)
class TailscalePeer:
    name: str
    dns_name: str
    address: str
    os: str
    online: bool
    owner: str
    tagged: bool
    mine: bool

    @property
    def is_desktop(self) -> bool:
        return self.os.lower() in DESKTOP_OSES


@dataclass(frozen=True)
class TailscaleStatus:
    #: running, stopped, needs_login, starting, not_installed or unavailable.
    state: str
    detail: str = ""
    name: str = ""
    dns_name: str = ""
    address: str = ""
    owner: str = ""
    tailnet: str = ""
    peers: Tuple[TailscalePeer, ...] = ()

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass(frozen=True)
class PeerIdentity:
    node: str
    owner: str
    tagged: bool


def is_tailscale_address(address) -> bool:
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return any(ip in network for network in _TAILSCALE_NETWORKS)


def find_cli() -> Optional[str]:
    """The ``tailscale`` executable, on PATH or where the installers put it."""
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = []
    if sys.platform == "win32":
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if root:
                candidates.append(os.path.join(root, "Tailscale", "tailscale.exe"))
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
            "/opt/homebrew/bin/tailscale",
            "/usr/local/bin/tailscale",
        ]
    else:
        candidates += ["/usr/bin/tailscale", "/usr/local/bin/tailscale", "/snap/bin/tailscale"]
    return next((path for path in candidates if os.path.isfile(path)), None)


def _run_json(args, timeout: float) -> dict:
    cli = find_cli()
    if cli is None:
        raise FileNotFoundError("tailscale")
    try:
        completed = subprocess.run(
            [cli, *args],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise TailscaleError("Tailscale didn't answer in time") from exc
    except OSError as exc:
        raise TailscaleError(f"Couldn't run Tailscale: {exc}") from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise TailscaleError(message[0] if message else "Tailscale isn't running")
    try:
        data = json.loads(completed.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise TailscaleError("Tailscale's answer was unreadable") from exc
    if not isinstance(data, dict):
        raise TailscaleError("Tailscale's answer was unreadable")
    return data


def _short_name(dns_name: str, fallback: str) -> str:
    label = dns_name.split(".", 1)[0] if dns_name else ""
    return label or fallback


def _ipv4(addresses) -> str:
    for address in addresses or ():
        if isinstance(address, str) and "." in address and ":" not in address:
            return address.split("/", 1)[0]
    return ""


def status(timeout: float = 4.0) -> TailscaleStatus:
    """This computer's Tailscale state and its peers. Never raises."""
    try:
        data = _run_json(["status", "--json"], timeout)
    except FileNotFoundError:
        return TailscaleStatus("not_installed")
    except TailscaleError as exc:
        return TailscaleStatus("unavailable", detail=str(exc))
    return parse_status(data)


def parse_status(data: dict) -> TailscaleStatus:
    state = _STATES.get(str(data.get("BackendState") or ""), "unavailable")
    users = data.get("User") if isinstance(data.get("User"), dict) else {}

    def owner_of(node: dict) -> str:
        user = users.get(str(node.get("UserID")))
        return str(user.get("LoginName") or "") if isinstance(user, dict) else ""

    me = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    my_owner = owner_of(me) if not me.get("Tags") else ""
    peers = []
    raw_peers = data.get("Peer") if isinstance(data.get("Peer"), dict) else {}
    for node in raw_peers.values():
        if not isinstance(node, dict):
            continue
        address = _ipv4(node.get("TailscaleIPs"))
        if not address:
            continue
        dns_name = str(node.get("DNSName") or "").rstrip(".")
        tagged = bool(node.get("Tags"))
        owner = "" if tagged else owner_of(node)
        peers.append(TailscalePeer(
            name=_short_name(dns_name, str(node.get("HostName") or address)),
            dns_name=dns_name,
            address=address,
            os=str(node.get("OS") or ""),
            online=bool(node.get("Online")),
            owner=owner,
            tagged=tagged,
            # Shared-in nodes belong to someone else even if the login matches.
            mine=bool(my_owner) and owner == my_owner and not node.get("ShareeNode"),
        ))
    peers.sort(key=lambda peer: (not peer.mine, peer.name.lower()))
    tailnet = data.get("CurrentTailnet") if isinstance(data.get("CurrentTailnet"), dict) else {}
    dns_name = str(me.get("DNSName") or "").rstrip(".")
    return TailscaleStatus(
        state=state,
        name=_short_name(dns_name, str(me.get("HostName") or "")),
        dns_name=dns_name,
        address=_ipv4(me.get("TailscaleIPs")),
        owner=my_owner,
        tailnet=str(tailnet.get("Name") or ""),
        peers=tuple(peers),
    )


def whois(address: str, timeout: float = 4.0) -> Optional[PeerIdentity]:
    """Which Tailscale node and user ``address`` belongs to, or None."""
    if not is_tailscale_address(address):
        return None
    try:
        data = _run_json(["whois", "--json", str(address)], timeout)
    except (FileNotFoundError, TailscaleError) as exc:
        logger.info("Tailscale whois for %s failed: %s", address, exc)
        return None
    node = data.get("Node") if isinstance(data.get("Node"), dict) else {}
    profile = data.get("UserProfile") if isinstance(data.get("UserProfile"), dict) else {}
    owner = str(profile.get("LoginName") or "")
    tagged = bool(node.get("Tags")) or owner == _TAGGED_OWNER
    name = str(node.get("ComputedName") or "") or _short_name(
        str(node.get("Name") or "").rstrip("."), str(address)
    )
    return PeerIdentity(node=name, owner="" if tagged else owner, tagged=tagged)


def own_device(remote: str, local: str, owner: str) -> Optional[PeerIdentity]:
    """The peer behind a connection, if it's one of ``owner``'s own computers.

    Both ends have to be Tailscale addresses, so the connection came in over
    the tailnet, and ``whois`` has to name the same, untagged user.
    """
    if not owner or not is_tailscale_address(remote) or not is_tailscale_address(local):
        return None
    identity = whois(remote)
    if identity is None or identity.tagged or identity.owner != owner:
        return None
    return identity
