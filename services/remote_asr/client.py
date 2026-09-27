"""Client side of the remote engine: pairing and the request connection.

``RemoteConnection.request`` has the signature of ``SpeechProcess.request``
(services/local_asr/process.py), so ``RemoteSpeechBackend`` drives a host
exactly as ``LocalSpeechBackend`` drives its local worker.
"""
from __future__ import annotations

import hmac
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional

from services.remote_asr import protocol
from services.remote_asr.tls import client_context, peer_fingerprint

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 6.0
#: Per address when there are others to try after it, so a laptop away from
#: home doesn't wait the full timeout on its LAN address before Tailscale.
FALLBACK_CONNECT_TIMEOUT_S = 2.5
#: Waiting for ``ready``/``paired`` after the socket opens.
HANDSHAKE_TIMEOUT_S = 10.0
PROBE_TIMEOUT_S = 2.0
#: A paired connection pings the host this often, and closes when a pong
#: takes longer than the timeout (as websockets' own keepalive would).
KEEPALIVE_INTERVAL_S = 20.0
KEEPALIVE_TIMEOUT_S = 20.0
#: How often a request waiting on the host checks for a cancel, which keeps
#: the connection and so can't interrupt the read the way closing it did.
_REQUEST_POLL_S = 0.05


class RemoteEngineError(RuntimeError):
    """A user-facing reason the remote engine can't be used right now."""


class RemoteConnectionLost(RemoteEngineError):
    """The connection dropped. ``sent`` says whether the host got the request."""

    def __init__(self, message: str, *, sent: bool):
        super().__init__(message)
        self.sent = sent


@dataclass(frozen=True)
class PairingResult:
    token: str
    device_id: str
    host_name: str
    fingerprint: str
    #: The host's other addresses, tried when the paired one is out of reach.
    alternates: tuple = ()
    via: str = "code"


@dataclass(frozen=True)
class ProbeResult:
    host_name: str
    protocol: int
    engine: dict
    tailscale_pairing: bool

    @property
    def compatible(self) -> bool:
        return self.protocol == protocol.PROTOCOL_VERSION


def _uri(host: str, port: int) -> str:
    return f"wss://{protocol.format_address(host, port)}{protocol.PATH}"


def _open(host: str, port: int, timeout: float):
    """Open the TLS WebSocket, translating failures into plain language."""
    from websockets.exceptions import InvalidHandshake, InvalidStatus
    from websockets.sync.client import connect

    where = protocol.format_address(host, port)
    try:
        return connect(
            _uri(host, port),
            ssl=client_context(),
            open_timeout=timeout,
            close_timeout=2,
            max_size=protocol.MAX_REPLY_BYTES,
            compression=None,
            # RemoteConnection keeps a paired connection alive itself (see
            # _heartbeat); probes and pairing close within seconds.
            ping_interval=None,
            ping_timeout=None,
            logger=logging.getLogger("websockets.remote_engine"),
        )
    except InvalidStatus as exc:
        raise RemoteEngineError(
            f"{where} answered, but it isn't an OpenWhisper remote engine "
            f"(HTTP {exc.response.status_code})."
        ) from exc
    except InvalidHandshake as exc:
        raise RemoteEngineError(f"{where} isn't an OpenWhisper remote engine.") from exc
    except socket.gaierror as exc:
        raise RemoteEngineError(f"Couldn't find a computer named {host}.") from exc
    except (OSError, TimeoutError) as exc:
        raise RemoteEngineError(
            f"Couldn't reach {where}. Check that the host is on, sharing its "
            "engine, and allowed through its firewall."
        ) from exc


def _quiet_close(ws) -> None:
    """Close without letting a slow close handshake hide the real error."""
    try:
        ws.close()
    except Exception:
        logger.debug("Remote engine socket close raised", exc_info=True)


def _read_json(ws, timeout: float) -> dict:
    from websockets.exceptions import ConnectionClosed

    try:
        raw = ws.recv(timeout=timeout)
    except TimeoutError as exc:
        raise RemoteEngineError("The host didn't answer in time.") from exc
    except ConnectionClosed as exc:
        raise RemoteEngineError("The host closed the connection.") from exc
    try:
        message = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RemoteEngineError("The host sent a reply this version can't read.") from exc
    if not isinstance(message, dict):
        raise RemoteEngineError("The host sent a reply this version can't read.")
    return message


def probe_host(host: str, port: int = protocol.DEFAULT_PORT, *,
               timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Ask whether ``host`` is sharing an engine, without pairing or a token."""
    ws = _open(host, port, timeout)
    try:
        ws.send(json.dumps({"type": "probe", "protocol": protocol.PROTOCOL_VERSION}))
        reply = _read_json(ws, timeout)
    finally:
        _quiet_close(ws)
    if reply.get("type") != "probe":
        raise RemoteEngineError(f"{protocol.format_address(host, port)} didn't answer the probe.")
    host_info = reply.get("host") if isinstance(reply.get("host"), dict) else {}
    engine = reply.get("engine") if isinstance(reply.get("engine"), dict) else {}
    try:
        version = int(reply.get("protocol"))
    except (TypeError, ValueError):
        version = 0
    return ProbeResult(
        host_name=str(host_info.get("name") or host),
        protocol=version,
        engine=engine,
        tailscale_pairing=reply.get("tailscale_pairing") is True,
    )


def pair_with_host(
    host: str,
    port: int,
    code: Optional[str],
    device_name: str,
    *,
    tailscale: bool = False,
    timeout: float = CONNECT_TIMEOUT_S,
) -> PairingResult:
    """Trade the code shown on the host for this computer's own token.

    With ``tailscale=True`` no code is sent: the host checks through
    Tailscale that this computer belongs to its owner. Either way, the
    certificate seen here is the one pinned from now on, so compare the
    fingerprint the host shows with ``PairingResult.fingerprint``.
    """
    request = {
        "type": "pair",
        "protocol": protocol.PROTOCOL_VERSION,
        "device_name": device_name,
    }
    if tailscale:
        request["tailscale"] = True
    else:
        code = "".join(ch for ch in str(code or "") if ch.isdigit())
        if not code:
            raise RemoteEngineError("Enter the pairing code shown on the host.")
        request["code"] = code
    ws = _open(host, port, timeout)
    try:
        fingerprint = peer_fingerprint(ws.socket)
        ws.send(json.dumps(request))
        reply = _read_json(ws, HANDSHAKE_TIMEOUT_S)
    finally:
        _quiet_close(ws)
    if reply.get("type") == "error":
        raise RemoteEngineError(str(reply.get("message") or "The host refused to pair."))
    token = reply.get("token")
    host_info = reply.get("host") if isinstance(reply.get("host"), dict) else {}
    if reply.get("type") != "paired" or not isinstance(token, str) or not token:
        raise RemoteEngineError("The host sent a pairing reply this version can't read.")
    advertised = str(host_info.get("fingerprint") or "")
    if advertised and not hmac.compare_digest(advertised.upper(), fingerprint):
        # The certificate we saw is not the one the host says it has: someone
        # is between the two computers.
        raise RemoteEngineError(
            "The host's certificate didn't match what it reported. Pairing was "
            "stopped; try again on a network you trust."
        )
    addresses = host_info.get("addresses") if isinstance(host_info.get("addresses"), list) else []
    return PairingResult(
        token=token,
        device_id=str(reply.get("device_id") or ""),
        host_name=str(host_info.get("name") or host),
        fingerprint=fingerprint,
        alternates=tuple(dict.fromkeys(
            str(a) for a in addresses if isinstance(a, str) and a and a != host
        )),
        via="tailscale" if tailscale else "code",
    )


class _WrongPlace(RemoteEngineError):
    """Nothing, or some other computer, answered at this address; try the next."""


class RemoteConnection:
    """One authenticated connection to a host; one request in flight at a time.

    ``alternates`` are the host's other addresses (its LAN and Tailscale
    IPs). They are tried in order when ``host`` doesn't answer, or answers
    with a different certificate, which is what a laptop away from home
    sees at its home LAN address. The token is only ever sent after the
    pinned certificate matches, whichever address answered.

    websockets reads close frames on its own thread, and ``_heartbeat``
    pings every 20 s, so ``alive``, ``latency`` and ``beats`` describe the
    socket between requests without anything more going over it.
    """

    #: The address that last worked for each host certificate, tried first
    #: next time so only the first connection pays for a dead address.
    _last_good: dict = {}
    _last_good_lock = threading.Lock()

    def __init__(self, host: str, port: int, token: str, fingerprint: str, *,
                 alternates=(), timeout: float = CONNECT_TIMEOUT_S):
        self.host = host
        self.port = port
        self._hosts = tuple(dict.fromkeys([host, *[a for a in alternates if a]]))
        self._token = token
        self._fingerprint = (fingerprint or "").upper()
        self._timeout = timeout
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._serial = 0
        #: Requests up to this serial were given up on; their replies are skipped.
        self._abandoned = 0
        self._closed = False
        self._stopped = threading.Event()
        self._ws = None
        self._rtt: Optional[float] = None
        self._beats = 0
        self.ready: dict = {}

    @property
    def where(self) -> str:
        return protocol.format_address(self.host, self.port)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def alive(self) -> bool:
        """Open as far as this computer can tell, without sending anything.

        A host that quits or stops sharing closes the socket at once; one
        that sleeps or leaves the network is noticed when a keepalive ping
        goes unanswered, 20 to 40 s later.
        """
        from websockets.protocol import State

        ws = self._ws
        return not self._closed and ws is not None and ws.state is State.OPEN

    @property
    def closed_for_engine_change(self) -> bool:
        """The host closed this connection because it switched engines."""
        ws = self._ws
        try:
            return ws is not None and ws.close_code == protocol.CLOSE_ENGINE_CHANGED
        except Exception:
            return False

    @property
    def latency(self) -> Optional[float]:
        """The last keepalive's round trip in seconds; None before one returns."""
        return self._rtt

    @property
    def beats(self) -> int:
        """Keepalive pongs so far: one right after connecting, then every 20 s."""
        return self._beats

    @property
    def via_tailscale(self) -> bool:
        """Whether the address that answered is on the tailnet.

        Judged by the peer's IP rather than the name paired with, which may
        be a MagicDNS name or a LAN host name.
        """
        from services.remote_asr.tailscale import is_tailscale_address

        ws = self._ws
        try:
            peer = ws.remote_address[0] if ws is not None else self.host
        except (AttributeError, IndexError, OSError, TypeError):
            peer = self.host
        return is_tailscale_address(peer)

    def _candidates(self) -> tuple:
        with self._last_good_lock:
            preferred = self._last_good.get(self._fingerprint)
        if preferred in self._hosts:
            return (preferred, *[host for host in self._hosts if host != preferred])
        return self._hosts

    def connect(self) -> dict:
        """Open, verify the pinned certificate, authenticate. Returns ``ready``."""
        candidates = self._candidates()
        errors = {}
        for index, host in enumerate(candidates):
            final = index == len(candidates) - 1
            timeout = self._timeout if final else min(self._timeout, FALLBACK_CONNECT_TIMEOUT_S)
            try:
                reply = self._connect_to(host, timeout)
            except _WrongPlace as exc:
                errors[host] = exc
                if not final:
                    logger.info("Remote engine not at %s (%s); trying %s",
                                host, exc, candidates[index + 1])
                continue
            if len(candidates) > 1:
                with self._last_good_lock:
                    self._last_good[self._fingerprint] = host
            return reply
        # Report the address the user paired with; the others were a bonus.
        raise errors.get(self._hosts[0]) or next(iter(errors.values()))

    def _connect_to(self, host: str, timeout: float) -> dict:
        try:
            ws = _open(host, self.port, timeout)
        except RemoteEngineError as exc:
            raise _WrongPlace(str(exc)) from exc
        where = protocol.format_address(host, self.port)
        try:
            seen = peer_fingerprint(ws.socket)
            if not self._fingerprint or not hmac.compare_digest(seen, self._fingerprint):
                raise _WrongPlace(
                    f"{where} presented a different certificate than when this "
                    "computer paired with it. If OpenWhisper was reinstalled there, "
                    "pair again; otherwise something is intercepting the connection."
                )
            ws.send(json.dumps({
                "type": "hello",
                "protocol": protocol.PROTOCOL_VERSION,
                "token": self._token,
            }))
            reply = _read_json(ws, HANDSHAKE_TIMEOUT_S)
            if reply.get("type") == "error":
                raise RemoteEngineError(str(reply.get("message") or "The host refused the connection."))
            if reply.get("type") != "ready":
                raise RemoteEngineError("The host sent a reply this version can't read.")
        except BaseException:
            _quiet_close(ws)
            raise
        with self._state_lock:
            if self._closed:
                _quiet_close(ws)
                raise RuntimeError("Transcription canceled")
            self._ws = ws
            self.host = host
        self.ready = reply
        threading.Thread(
            target=self._heartbeat, args=(ws,), name="remote-engine-keepalive", daemon=True,
        ).start()
        return reply

    def _heartbeat(self, ws) -> None:
        """Ping now and every KEEPALIVE_INTERVAL_S, timing each pong.

        This stands in for websockets' own keepalive, which times pongs with
        time.monotonic(): on Windows before Python 3.13 that ticks every
        15.6 ms, so a home network's round trip always read 0. A pong missing
        for KEEPALIVE_TIMEOUT_S closes the connection, as websockets' would.
        """
        from websockets.protocol import State

        while not self._stopped.is_set():
            started = time.perf_counter()
            try:
                try:
                    pong = ws.ping(ack_on_close=True)
                except TypeError:  # websockets before ack_on_close
                    pong = ws.ping()
            except Exception:
                return  # closed
            answered = pong.wait(KEEPALIVE_TIMEOUT_S)
            if self._stopped.is_set() or ws.state is not State.OPEN:
                return
            if not answered:
                logger.info("Remote engine at %s missed a keepalive ping; closing", self.where)
                self._mark_closed()
                return
            with self._state_lock:
                self._rtt = time.perf_counter() - started
                self._beats += 1
            self._stopped.wait(KEEPALIVE_INTERVAL_S)

    def request(self, op: str, *, timeout: float = 180.0, audio=None, **fields) -> dict:
        return self.request_timed(op, timeout=timeout, audio=audio, **fields)[0]

    def request_timed(self, op: str, *, timeout: float = 180.0, audio=None,
                      **fields) -> tuple[dict, Optional[float]]:
        """``request``, plus the seconds the host says it spent on it.

        The host's time is None from a host too old to report it.
        """
        from websockets.exceptions import ConnectionClosed

        with self._lock:
            with self._state_lock:
                if self._closed or self._ws is None:
                    raise RuntimeError("Transcription canceled")
                ws = self._ws
                self._serial += 1
                serial = self._serial
            try:
                ws.send(protocol.pack_request({"id": serial, "op": op, **fields}, audio))
            except (ConnectionClosed, OSError) as exc:
                self._mark_closed()
                raise RemoteConnectionLost(
                    f"Lost the connection to {self.where}.", sent=False
                ) from exc
            deadline = time.monotonic() + timeout
            while True:
                if self._abandoned >= serial:
                    raise RuntimeError("Transcription canceled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise RuntimeError(
                        f"The remote engine on {self.where} timed out; the connection was closed."
                    )
                try:
                    raw = ws.recv(timeout=min(_REQUEST_POLL_S, remaining))
                except TimeoutError:
                    if self._closed:
                        raise RuntimeError("Transcription canceled")
                    continue
                except (ConnectionClosed, OSError) as exc:
                    if self._closed:
                        raise RuntimeError("Transcription canceled")
                    self._mark_closed()
                    raise RemoteConnectionLost(
                        f"Lost the connection to {self.where}.", sent=True
                    ) from exc
                if self._closed:
                    raise RuntimeError("Transcription canceled")
                try:
                    reply = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                # A reply to an abandoned request arrives late and is skipped
                # here, by the next request, since its id is older.
                if not isinstance(reply, dict) or reply.get("id") != serial:
                    continue
                if reply.get("code") == "engine_changed":
                    self._mark_closed()
                    raise RemoteConnectionLost(str(reply.get("error")), sent=True)
                if "error" in reply:
                    raise RuntimeError(str(reply["error"]))
                result = reply.get("result")
                host_ms = reply.get("host_ms")
                host_s = host_ms / 1000 if isinstance(host_ms, (int, float)) and host_ms >= 0 else None
                return (result if isinstance(result, dict) else {}), host_s

    def abandon(self) -> None:
        """Stop waiting for the request in flight, but keep the connection.

        What a cancel does: the host finishes that request either way, since
        a disconnect doesn't interrupt a decode, so closing would only cost
        the next dictation a reconnect.
        """
        with self._state_lock:
            self._abandoned = self._serial

    def _mark_closed(self) -> None:
        with self._state_lock:
            self._closed = True
            ws, self._ws = self._ws, None
        self._stopped.set()
        if ws is not None:
            _quiet_close(ws)

    def close(self) -> None:
        """Drop the connection; a request waiting on another thread is canceled."""
        with self._state_lock:
            if self._closed:
                return
        self._mark_closed()
