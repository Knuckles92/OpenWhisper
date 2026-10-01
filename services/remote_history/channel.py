from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field

from services.remote_asr import protocol

logger = logging.getLogger(__name__)
QUERY_TIMEOUT = 5.0


class ClientUnavailable(RuntimeError):
    def __init__(self, code="unavailable"):
        if not isinstance(code, str) or code not in {
            "unavailable",
            "sharing_disabled",
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
                "not_found": "Record not found on this client.",
                "invalid_query": "Invalid client history query.",
                "snapshot_unavailable": "This client's saved insights are unavailable.",
                "unsupported": "Direct client queries are not available on this server.",
            }.get(code, "Client history is unavailable. The client may be offline.")
        )


@dataclass
class _Peer:
    ws: object
    enabled: bool
    pending: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def query(self, operation, params):
        event = threading.Event()
        request_id = uuid.uuid4().hex
        with self.lock:
            if not self.enabled:
                raise ClientUnavailable("sharing_disabled")
            if len(self.pending) >= 8:
                raise ClientUnavailable()
            response = {}
            self.pending[request_id] = (event, response)
        try:
            self.ws.send(
                json.dumps(
                    {
                        "type": "history_query",
                        "id": request_id,
                        "operation": operation,
                        "params": params,
                    }
                )
            )
            if not event.wait(QUERY_TIMEOUT):
                raise ClientUnavailable()
            if response.get("error"):
                raise ClientUnavailable(response["error"])
            return response["result"]
        except (KeyError, OSError) as exc:
            raise ClientUnavailable() from exc
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def disconnect(self):
        with self.lock:
            self.enabled = False
            for event, response in self.pending.values():
                response["error"] = "unavailable"
                event.set()


class HistoryBroker:
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

    def serve(self, ws, device, token, enabled):
        peer = _Peer(ws, enabled is True)
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
                if not isinstance(reply, dict) or reply.get("type") != "history_result":
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
