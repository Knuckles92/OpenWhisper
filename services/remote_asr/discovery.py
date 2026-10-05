"""Find OpenWhisper hosts on this network without Tailscale.

A computer that shares its engine answers a small UDP query on
``DISCOVERY_PORT`` (the engine's default port number, over UDP) with who it
is: its name, engine, TCP port, certificate fingerprint, and whether it takes
pairing requests. A computer looking for one broadcasts the query on each of
its network cards and collects the answers for about a second.

Nothing in an answer is secret: any TLS connection shows the fingerprint, and
the probe already says the rest. Nothing in one is trusted either. Pairing
pins the certificate, and a paired computer sends its token only after the
certificate matches, so a forged answer can at worst list a computer that then
fails to connect.

Windows Firewall lets unicast answers to a broadcast in for three seconds by
default, so the searching computer needs no rule of its own; the host's was
granted when it started listening for paired computers.

Some routers drop broadcasts between Wi-Fi and wired devices (mesh systems,
guest networks). ``sweep`` covers that: it knocks on the engine's TCP port at
every address of this computer's own /24, and only when someone asks for it.

The responder never answers with more bytes than the query held (queries are
padded to ``QUERY_BYTES``), answers only private, link-local, loopback and
Tailscale addresses, and limits how often it answers each of them.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

from services.remote_asr import protocol

logger = logging.getLogger(__name__)

#: The UDP port hosts answer on. Tests set it to 0 (any free port).
DISCOVERY_PORT = protocol.DEFAULT_PORT
QUERY_MAGIC = b"OWHISPER?1"
REPLY_MAGIC = b"OWHISPER!1"
NONCE_BYTES = 16
#: Every query is this long, and no answer is longer.
QUERY_BYTES = 512
#: How long a search listens for answers.
SEARCH_S = 1.2
#: When, within a search, the query goes out (UDP may drop any one of them).
_SEND_AT = (0.0, 0.3, 0.7)
#: Answers per sender address, and in all, within ``_RATE_WINDOW_S``.
_MAX_ANSWERS_PER_SENDER = 20
_MAX_ANSWERS = 200
_RATE_WINDOW_S = 10.0
SWEEP_CONNECT_S = 0.4
_SWEEP_WORKERS = 64
#: Networks a sweep covers at most (the first private LAN cards).
_SWEEP_NETWORKS = 2
_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_MAX_NAME = 60

Target = Union[str, Tuple[str, int]]


@dataclass(frozen=True)
class LanHost:
    """A computer on this network that answered: sharing, and who it is."""

    address: str
    port: int
    host_name: str
    fingerprint: str
    engine: dict
    protocol: int
    #: Takes pairing requests that its owner allows on its own screen.
    approval: bool

    @property
    def where(self) -> str:
        return protocol.format_address(self.address, self.port)

    @property
    def compatible(self) -> bool:
        return self.protocol == protocol.PROTOCOL_VERSION


# ---- wire format ----

def make_query(nonce: bytes) -> bytes:
    body = QUERY_MAGIC + nonce
    return body + b"\0" * (QUERY_BYTES - len(body))


def parse_query(data: bytes) -> Optional[bytes]:
    """The nonce of a well-formed query, else None (short ones included)."""
    if len(data) < QUERY_BYTES or not data.startswith(QUERY_MAGIC):
        return None
    return bytes(data[len(QUERY_MAGIC):len(QUERY_MAGIC) + NONCE_BYTES])


def encode_reply(nonce: bytes, info: dict) -> bytes:
    """``info`` as an answer no longer than a query, dropping what doesn't fit."""
    engine = info.get("engine") if isinstance(info.get("engine"), dict) else {}
    reply = {
        "n": nonce.hex(),
        "name": str(info.get("name") or "")[:_MAX_NAME],
        "port": int(info.get("port") or 0),
        "fp": str(info.get("fingerprint") or ""),
        "protocol": protocol.PROTOCOL_VERSION,
        "approval": info.get("approval") is True,
        "engine": {key: engine.get(key) for key in ("label", "device", "available", "family")},
    }
    for trim in (None, "engine", "name"):
        if trim == "engine":
            reply["engine"] = {"available": engine.get("available")}
        elif trim == "name":
            reply["name"] = reply["name"][:16]
        data = REPLY_MAGIC + json.dumps(reply, separators=(",", ":")).encode("utf-8")
        if len(data) <= QUERY_BYTES:
            return data
    return b""


def parse_reply(data: bytes, nonce: bytes, address: str) -> Optional[LanHost]:
    """An answer to the query sent with ``nonce``, else None."""
    if not data.startswith(REPLY_MAGIC):
        return None
    try:
        reply = json.loads(data[len(REPLY_MAGIC):].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(reply, dict) or reply.get("n") != nonce.hex():
        return None
    fingerprint = reply.get("fp")
    port = reply.get("port")
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(ch not in "0123456789abcdefABCDEF" for ch in fingerprint)):
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    try:
        version = int(reply.get("protocol"))
    except (TypeError, ValueError):
        version = 0
    engine = reply.get("engine") if isinstance(reply.get("engine"), dict) else {}
    name = "".join(ch for ch in str(reply.get("name") or "") if ch.isprintable()).strip()
    return LanHost(
        address=address,
        port=port,
        host_name=name[:_MAX_NAME] or address,
        fingerprint=fingerprint.upper(),
        engine=engine,
        protocol=version,
        approval=reply.get("approval") is True,
    )


def allowed_sender(address: str) -> bool:
    """Whether a query from ``address`` gets an answer: this network's, never the internet's."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.is_multicast or ip.is_unspecified:
        return False
    return ip.is_private or ip.is_link_local or ip.is_loopback or (
        ip.version == 4 and ip in _TAILSCALE_NETWORK
    )


# ---- the host's side ----

class DiscoveryResponder:
    """Answers discovery queries while this computer shares its engine.

    ``info`` returns what an answer says: ``name``, ``port``, ``fingerprint``,
    ``engine`` (the probe's subset) and ``approval``.
    """

    def __init__(self, info: Callable[[], dict]):
        self._info = info
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._per_sender: Dict[str, deque] = {}
        self._all: deque = deque()
        self.port: Optional[int] = None
        #: Why it isn't answering, when it couldn't start.
        self.error = ""

    @property
    def running(self) -> bool:
        return self._sock is not None

    def start(self, port: Optional[int] = None, bind: str = "0.0.0.0") -> bool:
        """Listen for queries. False (with ``error`` set) when the port is taken."""
        if self._sock is not None:
            return True
        port = DISCOVERY_PORT if port is None else port
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((bind, port))
        except OSError as exc:
            sock.close()
            self.error = f"UDP port {port} is in use ({exc.strerror or exc})"
            logger.warning("Remote engine discovery could not listen on UDP %s: %s", port, exc)
            return False
        sock.settimeout(0.5)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self.error = ""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, args=(sock,), name="RemoteEngineDiscovery", daemon=True
        )
        self._thread.start()
        logger.info("Remote engine discovery answering on UDP %s:%s", bind, self.port)
        return True

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        thread, self._thread = self._thread, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _serve(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                data, sender = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                # Closed by stop(), or (Windows) an ICMP "port unreachable"
                # left over from an earlier answer; neither ends the loop
                # unless stop() asked.
                if self._stop.is_set():
                    return
                time.sleep(0.05)
                continue
            try:
                reply = self.answer(data, sender[0])
                if reply:
                    sock.sendto(reply, sender)
            except OSError:
                logger.debug("Could not answer a discovery query", exc_info=True)
            except Exception:
                logger.warning("Remote engine discovery answer failed", exc_info=True)

    def answer(self, data: bytes, sender: str) -> Optional[bytes]:
        """The bytes to send back to ``sender``, or None to stay quiet."""
        nonce = parse_query(data)
        if nonce is None or not allowed_sender(sender) or not self._admit(sender):
            return None
        reply = encode_reply(nonce, self._info())
        return reply or None

    def _admit(self, sender: str) -> bool:
        now = time.monotonic()
        with self._lock:
            for times in (self._all, self._per_sender.setdefault(sender, deque())):
                while times and now - times[0] > _RATE_WINDOW_S:
                    times.popleft()
            mine = self._per_sender[sender]
            if len(mine) >= _MAX_ANSWERS_PER_SENDER or len(self._all) >= _MAX_ANSWERS:
                return False
            mine.append(now)
            self._all.append(now)
            if len(self._per_sender) > 1024:
                # Forget senders with nothing recent, so the map stays small.
                for key in [key for key, times in self._per_sender.items() if not times]:
                    del self._per_sender[key]
            return True


# ---- the searching side ----

def broadcast_targets() -> List[str]:
    """Each private LAN card's broadcast address, then the all-networks one.

    Windows sends 255.255.255.255 out of one card only, so each card's own
    broadcast address is what reaches the computers beside it.
    """
    from services.lan_address import LAN, local_ipv4_addresses

    targets = []
    for entry in local_ipv4_addresses():
        if entry.kind != LAN or not entry.private or not 8 <= entry.prefix <= 30:
            continue
        network = ipaddress.ip_network(f"{entry.address}/{entry.prefix}", strict=False)
        targets.append(str(network.broadcast_address))
    targets.append("255.255.255.255")
    return list(dict.fromkeys(targets))


def search(timeout: float = SEARCH_S, *, targets: Optional[Iterable[Target]] = None,
           stop_when: Optional[Callable[[LanHost], bool]] = None) -> List[LanHost]:
    """Computers on this network sharing an engine, one entry per certificate.

    Blocks for ``timeout`` (less once ``stop_when`` accepts an answer).
    ``targets`` are addresses, or (address, port) pairs, to query instead
    of this network's broadcast addresses.
    """
    if targets is None:
        try:
            plain = broadcast_targets()
        except Exception:
            logger.debug("Could not list broadcast addresses", exc_info=True)
            plain = []
        targets = plain
    destinations = [
        (target, DISCOVERY_PORT) if isinstance(target, str) else (str(target[0]), int(target[1]))
        for target in targets
    ]
    destinations = [d for d in destinations if d[1]]
    if not destinations:
        return []
    nonce = secrets.token_bytes(NONCE_BYTES)
    query = make_query(nonce)
    found: Dict[str, LanHost] = {}
    sends = list(_SEND_AT)
    # No bind(): the first sendto picks a port, as any client's would, so
    # searching never registers this app as something that listens.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            while sends and sends[0] <= elapsed:
                sends.pop(0)
                for destination in destinations:
                    try:
                        sock.sendto(query, destination)
                    except OSError:
                        logger.debug("Discovery query to %s failed", destination, exc_info=True)
            remaining = timeout - elapsed
            if remaining <= 0:
                break
            sock.settimeout(max(0.01, min(remaining, (sends[0] - elapsed) if sends else remaining)))
            try:
                data, sender = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                # Windows: an ICMP "port unreachable" from a target with no host.
                continue
            host = parse_reply(data, nonce, sender[0])
            if host is None or host.fingerprint in found:
                continue
            found[host.fingerprint] = host
            if stop_when is not None and stop_when(host):
                break
    return list(found.values())


def find_host(fingerprint: str, timeout: float = SEARCH_S) -> Optional[LanHost]:
    """Where the host with this certificate answers on this network now, if it does."""
    wanted = (fingerprint or "").upper()
    if not wanted:
        return None
    hosts = search(timeout, stop_when=lambda host: host.fingerprint == wanted)
    return next((host for host in hosts if host.fingerprint == wanted), None)


def sweep_addresses() -> List[str]:
    """Every other address of this computer's private /24s (its first two networks)."""
    from services.lan_address import LAN, local_ipv4_addresses

    networks = []
    addresses = []
    for entry in local_ipv4_addresses():
        if entry.kind != LAN or not entry.private:
            continue
        network = ipaddress.ip_network(f"{entry.address}/{max(entry.prefix or 24, 24)}", strict=False)
        if network in networks:
            continue
        networks.append(network)
        addresses.extend(str(ip) for ip in network.hosts() if str(ip) != entry.address)
        if len(networks) >= _SWEEP_NETWORKS:
            break
    return list(dict.fromkeys(addresses))


def sweep(port: int = protocol.DEFAULT_PORT, *, addresses: Optional[Iterable[str]] = None,
          connect_timeout: float = SWEEP_CONNECT_S) -> List[str]:
    """Addresses here that accept a connection on ``port``. Blocks about two seconds.

    Only a TCP connect, closed at once: whatever answers is then probed
    the usual way, which says whether it's OpenWhisper.
    """
    candidates = sweep_addresses() if addresses is None else list(addresses)
    if not candidates:
        return []

    def knock(address: str) -> Optional[str]:
        try:
            with socket.create_connection((address, port), timeout=connect_timeout):
                return address
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=min(_SWEEP_WORKERS, len(candidates))) as pool:
        return [address for address in pool.map(knock, candidates) if address]
