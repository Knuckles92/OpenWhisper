from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field

from services.remote_asr import protocol

logger = logging.getLogger(__name__)
QUERY_TIMEOUT = 5.0
#: A live tool waits on the client's app, which answers within a few seconds.
TOOL_TIMEOUT = 10.0
#: What ``HistoryBroker.live_clients`` says of each paired computer.
LIVE_AVAILABLE, LIVE_DISABLED, LIVE_UNSUPPORTED, LIVE_OFFLINE = (
    "available", "disabled", "unsupported", "offline",
)


class ClientUnavailable(RuntimeError):
    def __init__(self, code="unavailable"):
        if not isinstance(code, str) or code not in {
            "unavailable",
            "sharing_disabled",
            "live_disabled",
            "not_found",
            "invalid_query",
            "snapshot_unavailable",
            "unsupported",
        }:
            code = "unavailable"
        self.code = code
        super().__init__(
            {
                "sharing_disabled": "History sharing is disabled on this client.",
                "live_disabled": (
                    "This client does not allow live access through the host's MCP. "
                    "Its user can allow it in that computer's Settings > API & MCP."
                ),
                "not_found": "Record not found on this client.",
                "invalid_query": "Invalid client history query.",
                "snapshot_unavailable": "This client's saved insights are unavailable.",
                "unsupported": "Direct client queries are not available on this server.",
            }.get(code, "Client history is unavailable. The client may be offline.")
        )


class ClientToolError(RuntimeError):
    """A paired client's refusal of a live tool, as ``code: message`` text."""


def _tool_refusal(error):
    """A client's ``error`` reply, kept to a short single ``code: message`` line."""
    text = " ".join(error.split())[:500] if isinstance(error, str) else ""
    return text if ": " in text else "unavailable: The client could not do that right now."


@dataclass
class _Peer:
    ws: object
    enabled: bool
    #: The client's opt-in to live tools, or None when its version has none.
    live: bool | None = None
    pending: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False

    def _ask(self, message, timeout):
        """The client's reply to ``message``, matched by a fresh id."""
        event = threading.Event()
        request_id = uuid.uuid4().hex
        with self.lock:
            if self.closed or len(self.pending) >= 8:
                raise ClientUnavailable()
            response = {}
            self.pending[request_id] = (event, response)
        try:
            self.ws.send(json.dumps({**message, "id": request_id}))
            if not event.wait(timeout):
                raise ClientUnavailable()
            return response
        except OSError as exc:
            raise ClientUnavailable() from exc
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def query(self, operation, params):
        with self.lock:
            if not self.enabled:
                raise ClientUnavailable("sharing_disabled")
        response = self._ask(
            {"type": "history_query", "operation": operation, "params": params},
            QUERY_TIMEOUT,
        )
        if response.get("error") or "result" not in response:
            raise ClientUnavailable(response.get("error"))
        return response["result"]

    def call_tool(self, tool, arguments):
        with self.lock:
            live = self.live
        if live is None:
            raise ClientUnavailable("unsupported")
        if not live:
            raise ClientUnavailable("live_disabled")
        message = {"type": "live_call", "tool": tool, "arguments": arguments}
        # The client drops the connection over a larger frame; the id adds a little.
        if len(json.dumps(message)) > protocol.MAX_HEADER_BYTES - 64:
            raise ClientToolError(
                "invalid_parameters: These arguments are too large to send to a paired client."
            )
        response = self._ask(message, TOOL_TIMEOUT)
        if response.get("error") == "live_disabled":
            # Turned off on the client since it connected.
            with self.lock:
                self.live = False
        if response.get("error") in ("unavailable", "live_disabled"):
            raise ClientUnavailable(response["error"])
        if response.get("error") or not isinstance(response.get("result"), dict):
            raise ClientToolError(_tool_refusal(response.get("error")))
        return response["result"]

    def disconnect(self):
        with self.lock:
            self.enabled = False
            self.closed = True
            for event, response in self.pending.values():
                response["error"] = "unavailable"
                event.set()


def _live_status(peer):
    if peer is None:
        return LIVE_OFFLINE
    if peer.live is None:
        return LIVE_UNSUPPORTED
    return LIVE_AVAILABLE if peer.live else LIVE_DISABLED


class HistoryBroker:
    """The host's end of each paired client's side connection.

    Clients open it; the host sends read-only history queries and, to clients
    that allow it, live tool calls. Neither carries audio.
    """

    def __init__(self, registry):
        self.registry = registry
        self._lock = threading.Lock()
        self._peers = {}
        self._accepting = False

    def start(self):
        with self._lock:
            self._accepting = True

    def clients(self):
        with self._lock:
            return [
                {
                    "device_id": device["id"],
                    "name": device["name"],
                    "status": (
                        "online"
                        if self._peers[device["id"]].enabled
                        else "sharing_disabled"
                    )
                    if device["id"] in self._peers
                    else "unavailable",
                }
                for device in self.registry.list()
            ]

    def query(self, device_id, operation, params):
        with self._lock:
            peer = self._peers.get(device_id)
        if peer is None or not any(d["id"] == device_id for d in self.registry.list()):
            raise ClientUnavailable()
        try:
            return peer.query(operation, params)
        except ClientUnavailable:
            raise
        except Exception as exc:
            raise ClientUnavailable() from exc

    def live_clients(self):
        """Each paired computer's id, name and whether live tools can run on it now."""
        with self._lock:
            return [
                {
                    "device_id": device["id"],
                    "name": device["name"],
                    "live": _live_status(self._peers.get(device["id"])),
                }
                for device in self.registry.list()
            ]

    def call_tool(self, device_id, tool, arguments):
        """Run a live tool on a paired client and return its result.

        ``ClientUnavailable`` when the client is offline, too old or has not
        allowed live access; ``ClientToolError`` with the client's own refusal.
        """
        with self._lock:
            peer = self._peers.get(device_id)
        if peer is None or not any(d["id"] == device_id for d in self.registry.list()):
            raise ClientUnavailable()
        try:
            return peer.call_tool(tool, arguments)
        except (ClientUnavailable, ClientToolError):
            raise
        except Exception as exc:
            raise ClientUnavailable() from exc

    def serve(self, ws, device, token, enabled, live=None):
        peer = _Peer(ws, enabled is True, live if isinstance(live, bool) else None)
        with self._lock:
            if not self._accepting:
                ws.close(1001, "host stopped sharing")
                return
            previous = self._peers.get(device["id"])
            self._peers[device["id"]] = peer
        if previous is not None:
            previous.disconnect()
            previous.ws.close(1001, "history connection replaced")
        try:
            ws.send(
                json.dumps({"type": "ready", "capabilities": {"client_history": True}})
            )
            for frame in ws:
                if self.registry.authenticate(token) is None:
                    break
                if (
                    not isinstance(frame, str)
                    or len(frame.encode("utf-8")) > protocol.MAX_REPLY_BYTES
                ):
                    break
                reply = json.loads(frame)
                if not isinstance(reply, dict) or reply.get("type") not in (
                    "history_result",
                    "live_result",
                ):
                    break
                with peer.lock:
                    pending = peer.pending.get(reply.get("id"))
                    if pending:
                        event, response = pending
                        response.update(
                            {k: reply[k] for k in ("result", "error") if k in reply}
                        )
                        event.set()
        except Exception:
            logger.debug("Client history connection closed", exc_info=True)
        finally:
            peer.disconnect()
            with self._lock:
                if self._peers.get(device["id"]) is peer:
                    self._peers.pop(device["id"], None)
            ws.close()

    def remove(self, device_id=None):
        with self._lock:
            if device_id is None:
                self._accepting = False
            peers = [
                p for d, p in self._peers.items() if device_id is None or d == device_id
            ]
            for d in list(self._peers):
                if device_id is None or d == device_id:
                    self._peers.pop(d)
        for peer in peers:
            peer.disconnect()
            peer.ws.close(1001, "history connection closed")
