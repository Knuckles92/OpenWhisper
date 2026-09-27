"""This computer's address on the local network, told apart from a VPN's.

The route probe (a UDP connect toward a TEST-NET address, which sends
nothing) answers with whichever interface holds the default route. With a
VPN up, that is the tunnel: Proton VPN put 10.2.0.2 in the remote engine's
share line instead of the laptop's 192.168.0.118, an address the other
computers at home can't reach. So the interfaces are listed and each address
classified:

* ``lan``: an ordinary network card, wired or Wi-Fi.
* ``vpn``: a tunnel. Point-to-point links, Linux tun/WireGuard devices,
  Windows tunnel adapters, and adapters named for a VPN.
* ``virtual``: Docker, VM and WSL bridges, reachable only from this computer.
* ``tailscale``: the tailnet address, which the remote engine lists apart.

``best_lan_address`` prefers a private LAN address and falls back to a VPN
one, labelled as such; ``discover_lan_ipv4`` is the plain string for the
Meeting Mode dashboard. Everything here is stdlib plus ctypes (getifaddrs on
Linux and macOS, GetAdaptersAddresses on Windows) and never raises.
"""
from __future__ import annotations

import ctypes
import ipaddress
import logging
import os
import socket
import sys
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

LAN = "lan"
VPN = "vpn"
VIRTUAL = "virtual"
TAILSCALE = "tailscale"

_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")

# Interface names, lowercased prefixes.
_VPN_NAMES = (
    "tun", "tap", "wg", "ppp", "ipsec", "utun", "gpd", "proton", "nordlynx",
    "mullvad", "zt", "vpn", "cscotun", "ivpn", "pia",
)
_VIRTUAL_NAMES = (
    "docker", "br-", "veth", "virbr", "vmnet", "vboxnet", "lxcbr", "lxdbr",
    "podman", "cni", "flannel", "cali", "kube", "awdl", "llw", "anpi",
    "bridge", "vethernet",
)
# Windows adapter descriptions, lowercased substrings.
_VPN_DESCRIPTIONS = (
    "tap-windows", "wintun", "wireguard", "openvpn", "vpn", "proton", "nord",
    "mullvad", "anyconnect", "fortinet", "zerotier", "pangp", "juniper",
)
_VIRTUAL_DESCRIPTIONS = (
    "hyper-v", "vmware", "virtualbox", "wsl", "vethernet", "npcap loopback",
    "docker", "parallels",
)

# Windows IF_TYPE_* values (ipifcons.h).
_IF_TYPE_PPP = 23
_IF_TYPE_SOFTWARE_LOOPBACK = 24
_IF_TYPE_PROP_VIRTUAL = 53
_IF_TYPE_TUNNEL = 131

# Linux ARPHRD_* device types (/sys/class/net/<name>/type) for tunnels:
# PPP, IPIP, SIT, GRE, and "none" (tun and WireGuard).
_ARPHRD_TUNNELS = {512, 768, 776, 778, 65534}

_IFF_LOOPBACK = 0x8
_IFF_POINTOPOINT = 0x10


@dataclass(frozen=True)
class LocalAddress:
    address: str
    interface: str = ""
    kind: str = LAN
    prefix: int = 0
    #: A real network card (Linux: backed by a device; Windows: Ethernet or Wi-Fi).
    physical: bool = False

    @property
    def private(self) -> bool:
        return ipaddress.ip_address(self.address).is_private


@dataclass(frozen=True)
class _Interface:
    name: str
    address: str
    prefix: int
    point_to_point: bool = False
    description: str = ""
    #: Windows IfType, or None elsewhere.
    if_type: Optional[int] = None


def _usable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (ip.version == 4 and not ip.is_loopback and not ip.is_unspecified
            and not ip.is_link_local and not ip.is_multicast)


# ---- listing interfaces ----

def _prefix_length(mask: bytes) -> int:
    return bin(int.from_bytes(mask, "big")).count("1")


def _posix_interfaces() -> Optional[List[_Interface]]:
    """Every IPv4 address with its interface, from getifaddrs (Linux, macOS)."""
    import ctypes.util

    class _Ifaddrs(ctypes.Structure):
        pass

    _Ifaddrs._fields_ = [
        ("ifa_next", ctypes.POINTER(_Ifaddrs)),
        ("ifa_name", ctypes.c_char_p),
        ("ifa_flags", ctypes.c_uint),
        ("ifa_addr", ctypes.c_void_p),
        ("ifa_netmask", ctypes.c_void_p),
        ("ifa_ifu", ctypes.c_void_p),
        ("ifa_data", ctypes.c_void_p),
    ]
    libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    head = ctypes.POINTER(_Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        return None
    # BSD sockaddrs start with a length byte, then the family byte; Linux's
    # start with a two-byte family. AF_INET is 2 on both.
    bsd = sys.platform == "darwin"

    def family(pointer) -> int:
        raw = ctypes.string_at(pointer, 2)
        return raw[1] if bsd else int.from_bytes(raw, sys.byteorder)

    found = []
    try:
        node = head
        while node:
            entry = node.contents
            node = entry.ifa_next
            if not entry.ifa_addr or family(entry.ifa_addr) != socket.AF_INET:
                continue
            if entry.ifa_flags & _IFF_LOOPBACK:
                continue
            address = socket.inet_ntoa(ctypes.string_at(entry.ifa_addr + 4, 4))
            prefix = (_prefix_length(ctypes.string_at(entry.ifa_netmask + 4, 4))
                      if entry.ifa_netmask else 0)
            found.append(_Interface(
                name=(entry.ifa_name or b"").decode("utf-8", "replace"),
                address=address,
                prefix=prefix,
                point_to_point=bool(entry.ifa_flags & _IFF_POINTOPOINT),
            ))
    finally:
        libc.freeifaddrs(head)
    return found


def _windows_interfaces() -> Optional[List[_Interface]]:
    """Every IPv4 address with its adapter, from GetAdaptersAddresses."""
    from ctypes import wintypes

    class _SocketAddress(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]

    class _Unicast(ctypes.Structure):
        pass

    _Unicast._fields_ = [
        ("Length", wintypes.ULONG), ("Flags", wintypes.DWORD),
        ("Next", ctypes.POINTER(_Unicast)),
        ("Address", _SocketAddress),
        ("PrefixOrigin", ctypes.c_int), ("SuffixOrigin", ctypes.c_int),
        ("DadState", ctypes.c_int),
        ("ValidLifetime", wintypes.ULONG), ("PreferredLifetime", wintypes.ULONG),
        ("LeaseLifetime", wintypes.ULONG),
        ("OnLinkPrefixLength", ctypes.c_ubyte),
    ]

    class _Adapter(ctypes.Structure):
        pass

    # Only the fields up to OperStatus are read; the API's buffer holds the rest.
    _Adapter._fields_ = [
        ("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD),
        ("Next", ctypes.POINTER(_Adapter)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(_Unicast)),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("Flags", wintypes.ULONG),
        ("Mtu", wintypes.ULONG),
        ("IfType", wintypes.DWORD),
        ("OperStatus", ctypes.c_int),
    ]

    iphlpapi = ctypes.WinDLL("iphlpapi")
    skip = 0x2 | 0x4 | 0x8  # anycast, multicast, DNS servers
    size = wintypes.ULONG(16 * 1024)
    for _attempt in range(4):
        buffer = ctypes.create_string_buffer(size.value)
        result = iphlpapi.GetAdaptersAddresses(
            socket.AF_INET, skip, None, buffer, ctypes.byref(size)
        )
        if result == 111:  # ERROR_BUFFER_OVERFLOW: size now says how much
            continue
        break
    if result != 0:
        return None

    found = []
    node = ctypes.cast(buffer, ctypes.POINTER(_Adapter))
    while node:
        adapter = node.contents
        node = adapter.Next
        if adapter.OperStatus != 1 or adapter.IfType == _IF_TYPE_SOFTWARE_LOOPBACK:
            continue
        unicast = adapter.FirstUnicastAddress
        while unicast:
            entry = unicast.contents
            unicast = entry.Next
            pointer = entry.Address.lpSockaddr
            if not pointer:
                continue
            raw = ctypes.string_at(pointer, 8)
            if int.from_bytes(raw[:2], "little") != socket.AF_INET:
                continue
            found.append(_Interface(
                name=adapter.FriendlyName or "",
                address=socket.inet_ntoa(raw[4:8]),
                prefix=int(entry.OnLinkPrefixLength),
                description=adapter.Description or "",
                if_type=int(adapter.IfType),
            ))
    return found


def _interfaces() -> Optional[List[_Interface]]:
    """None when this platform's listing isn't available or failed."""
    try:
        if sys.platform == "win32":
            return _windows_interfaces()
        if sys.platform.startswith("linux") or sys.platform == "darwin":
            return _posix_interfaces()
    except Exception:
        logger.debug("Could not list network interfaces", exc_info=True)
    return None


# ---- classifying ----

def _sysfs(name: str, leaf: str) -> str:
    return os.path.join("/sys/class/net", name, leaf)


def _linux_kind(interface: _Interface) -> Optional[str]:
    """What sysfs says about a Linux interface, or None when it says nothing."""
    try:
        with open(_sysfs(interface.name, "type"), encoding="ascii") as handle:
            device_type = int(handle.read().strip() or 0)
    except (OSError, ValueError):
        return None
    if device_type in _ARPHRD_TUNNELS or os.path.exists(_sysfs(interface.name, "tun_flags")):
        return VPN
    return None


def _linux_physical(name: str) -> bool:
    return os.path.exists(_sysfs(name, "device"))


def classify(interface: _Interface) -> LocalAddress:
    """Label one interface address as LAN, VPN, virtual or Tailscale."""
    name = interface.name.lower()
    description = interface.description.lower()
    ip = ipaddress.ip_address(interface.address)
    physical = False
    if ip in _TAILSCALE_NETWORK or name.startswith("tailscale") or "tailscale" in description:
        kind = TAILSCALE
    elif interface.if_type is not None:
        # Windows: the adapter's type and description.
        if interface.if_type in (_IF_TYPE_PPP, _IF_TYPE_PROP_VIRTUAL, _IF_TYPE_TUNNEL) or any(
            word in description for word in _VPN_DESCRIPTIONS
        ):
            kind = VPN
        elif any(word in description for word in _VIRTUAL_DESCRIPTIONS) or name.startswith("vethernet"):
            kind = VIRTUAL
        else:
            kind = LAN
            physical = interface.if_type in (6, 71)  # Ethernet, Wi-Fi
    else:
        kind = None
        if sys.platform.startswith("linux"):
            kind = _linux_kind(interface)
        if kind is None:
            if interface.point_to_point or interface.prefix == 32 or name.startswith(_VPN_NAMES):
                kind = VPN
            elif name.startswith(_VIRTUAL_NAMES):
                kind = VIRTUAL
            else:
                kind = LAN
        if kind == LAN and sys.platform.startswith("linux"):
            physical = _linux_physical(interface.name)
    return LocalAddress(interface.address, interface.name, kind, interface.prefix, physical)


def _route_address() -> Optional[str]:
    """The address holding the default route, without sending anything."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return None


def _hostname_addresses() -> List[str]:
    try:
        return [info[4][0] for info in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM
        )]
    except OSError:
        return []


def local_ipv4_addresses() -> List[LocalAddress]:
    """This computer's usable IPv4 addresses, classified, best LAN first.

    Addresses the interface listing didn't cover (it failed, or this
    platform has none) come from the route probe and the host name, judged
    by address alone.
    """
    listed = _interfaces()
    route = _route_address()
    addresses: List[LocalAddress] = []
    seen = set()
    for interface in listed or ():
        if _usable(interface.address) and interface.address not in seen:
            seen.add(interface.address)
            addresses.append(classify(interface))
    for address in [route, *_hostname_addresses()]:
        if address and _usable(address) and address not in seen:
            seen.add(address)
            kind = TAILSCALE if ipaddress.ip_address(address) in _TAILSCALE_NETWORK else LAN
            addresses.append(LocalAddress(address, kind=kind))

    def rank(entry: LocalAddress):
        return (
            {LAN: 0, VPN: 1, TAILSCALE: 2, VIRTUAL: 3}[entry.kind],
            not entry.private,
            entry.address != route,  # the default route breaks ties
            not entry.physical,
        )

    return sorted(addresses, key=rank)


def best_lan_address() -> Optional[LocalAddress]:
    """Where other computers here can reach this one: a LAN address, else a VPN's.

    Never a Docker/VM bridge or the Tailscale address (listed separately).
    Check ``kind`` before offering a VPN address as a LAN one.
    """
    for entry in local_ipv4_addresses():
        if entry.kind in (LAN, VPN):
            return entry
    return None


def discover_lan_ipv4() -> Optional[str]:
    """``best_lan_address`` as a plain string, for links other devices open."""
    best = best_lan_address()
    return best.address if best is not None else None
