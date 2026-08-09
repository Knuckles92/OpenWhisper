"""WebSocket hub for the meeting dashboard.

One ``WsHub`` instance serves all dashboard connections of the running web
server. Connection bookkeeping happens exclusively on the server's asyncio
event loop; cross-thread producers (the state store's subscriber callback,
``MeetingWebServer.broadcast``) enter through ``schedule_broadcast``, which
marshals into the loop with ``asyncio.run_coroutine_threadsafe``.

Applied state patches are broadcast via the hub's subscription to
``engine.store`` — the action receive path only echoes an ``action_result``
to the sender and never re-broadcasts, so clients see each change exactly
once.

A connecting socket is registered *before* its ``hello`` snapshot is
assembled: broadcasts raised during that window are buffered per socket and
flushed immediately after ``hello``, so no mutation can fall between the
snapshot and the first live patch. Every blocking store/repository call is
pushed onto a worker thread, and every broadcast fans out concurrently with a
per-socket timeout so one stalled client cannot hold up the rest.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from concurrent.futures import CancelledError, Future
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from fastapi import WebSocket, WebSocketDisconnect

from meeting.interfaces import OpResult
from meeting.web.auth import resolve_role

logger = logging.getLogger(__name__)

#: Custom close codes (4000-4999 range is application-defined).
WS_CLOSE_NAME_REQUIRED = 4400
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_RESYNC = 4409
WS_CLOSE_TOO_MANY = 4429

#: Number of trailing transcript segments included in the ``hello`` message.
HELLO_SEGMENT_COUNT = 200

#: Largest inbound text frame accepted, in characters. Anything bigger is
#: rejected before ``json.loads`` ever sees it.
MAX_MESSAGE_CHARS = 64 * 1024

#: Dashboard sockets served concurrently by one hub.
MAX_CONNECTIONS = 32

#: Worker-thread actions one socket may have in flight. The receive loop is
#: sequential, so this is a guard rail: it bounds a socket's share of the
#: shared thread pool even if dispatch ever becomes concurrent.
MAX_INFLIGHT_ACTIONS = 4

#: Broadcasts buffered for a socket between registration and its ``hello``.
MAX_PENDING_MESSAGES = 512

#: Per-socket budget for a single broadcast send.
BROADCAST_SEND_TIMEOUT_S = 5.0

#: Consecutive broadcast timeouts tolerated before a socket is dropped.
MAX_SEND_TIMEOUTS = 3

#: Upper bound for a client-supplied event seq (undo).
MAX_EVENT_SEQ = 2 ** 63 - 1

#: Stable guest keys are opaque client-generated strings, never credentials.
_GUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _op_result_dict(result: OpResult) -> Dict[str, Any]:
    """Serialize an ``OpResult`` into the wire shape for ``action_result``."""
    return {
        "ok": result.ok,
        "reason": result.reason,
        "target_id": result.target_id,
        "seq": result.seq,
        "effect": result.effect,
    }


def _rejection(reason: str) -> List[Dict[str, Any]]:
    """A single-entry ``action_result`` results list for a local rejection."""
    return [{"ok": False, "reason": reason, "target_id": None,
             "seq": None, "effect": None}]


def _clean_guest_id(value: Optional[str]) -> Optional[str]:
    """Sanitize the client's stable guest key.

    Args:
        value: The raw ``guest_id`` query parameter.

    Returns:
        The key when it is a short opaque token, else None. The key is a
        convenience for reusing one participant across reconnects; the guest
        token remains the only authority.
    """
    candidate = (value or "").strip()
    return candidate if _GUEST_ID_RE.match(candidate) else None


def _coerce_seq(value: Any) -> Optional[int]:
    """Coerce a client-supplied event seq to a sane int, else None."""
    try:
        seq = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seq if 0 <= seq <= MAX_EVENT_SEQ else None


def _log_broadcast_failure(future: "Future") -> None:
    """Done-callback that surfaces broadcast failures instead of swallowing them."""
    try:
        error = future.exception()
    except CancelledError:
        return
    except Exception:
        logger.exception("Broadcast future could not be inspected")
        return
    if error is not None:
        logger.error("Dashboard broadcast failed: %s", error, exc_info=error)


class _Connection:
    """Per-socket bookkeeping for one dashboard client.

    Attributes:
        role: ``host`` or ``guest``.
        participant_id: The acting participant, when resolved.
        name: The display name the client connected with.
        ready: True once ``hello`` has been sent and buffering has stopped.
        pending: Broadcast payloads buffered before ``ready``.
        overflowed: True when ``pending`` exceeded its cap; the socket is
            closed so the client reconnects for a fresh snapshot.
        send_timeouts: Consecutive broadcast send timeouts.
        inflight: Worker-thread actions currently running for this socket.
    """

    __slots__ = ("role", "participant_id", "name", "ready", "pending",
                 "overflowed", "send_timeouts", "inflight")

    def __init__(self, role: str, name: str) -> None:
        self.role = role
        self.participant_id: Optional[str] = None
        self.name = name
        self.ready = False
        self.pending: Deque[str] = deque()
        self.overflowed = False
        self.send_timeouts = 0
        self.inflight = 0


class WsHub:
    """Connection registry and broadcast fan-out for dashboard WebSockets."""

    def __init__(self, engine: Any, repository: Any) -> None:
        """Args:
            engine: The ``MeetingEngine`` owning tokens, store, and actions.
            repository: A ``MeetingRepository`` for meeting/segment reads.
        """
        self._engine = engine
        self._repository = repository
        self._connections: Dict[WebSocket, _Connection] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribed_store: Optional[Any] = None
        #: Stable guest key -> (participant_id, display_name), so a guest that
        #: reconnects reuses its participant instead of minting a new one.
        self._guest_participants: Dict[str, Tuple[str, str]] = {}
        #: Set by ``MeetingWebServer`` so ``hello`` can carry an absolute
        #: guest URL; falls back to a relative ``/m/{token}`` path.
        self.get_guest_url: Optional[Callable[[], str]] = None

    # ------------------------------------------------------------------
    # Lifecycle (called from the server's event loop)
    # ------------------------------------------------------------------

    def on_startup(self) -> None:
        """Capture the serving loop and subscribe to the state store."""
        self._loop = asyncio.get_running_loop()
        self._ensure_store_subscription()

    def on_shutdown(self) -> None:
        """Unsubscribe from the state store and drop the loop reference."""
        store = self._subscribed_store
        if store is not None:
            try:
                store.unsubscribe(self._on_store_batch)
            except Exception:
                logger.exception("Failed to unsubscribe from state store")
            self._subscribed_store = None
        self._loop = None

    def _ensure_store_subscription(self) -> None:
        """Subscribe to ``engine.store`` when it exists (idempotent).

        The store is created by ``MeetingEngine.start()``, which may happen
        after the web server boots, so this is re-checked on every connect.
        """
        store = getattr(self._engine, "store", None)
        if store is None or store is self._subscribed_store:
            return
        if self._subscribed_store is not None:
            try:
                self._subscribed_store.unsubscribe(self._on_store_batch)
            except Exception:
                logger.exception("Failed to unsubscribe stale state store")
        store.subscribe(self._on_store_batch)
        self._subscribed_store = store

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    def _on_store_batch(self, seq: int, applied: List[OpResult]) -> None:
        """State-store subscriber: fan applied ops out as a ``patch``."""
        message = {
            "type": "patch",
            "seq": seq,
            "results": [
                {"op": r.op, "target_id": r.target_id,
                 "effect": r.effect, "seq": r.seq}
                for r in applied
            ],
        }
        self.schedule_broadcast(message)

    def schedule_broadcast(self, message: Dict[str, Any]) -> None:
        """Thread-safe entry point: queue ``message`` for broadcast.

        Safe to call from any thread; a no-op when the server loop is not
        running (before startup / after shutdown).
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.broadcast_json(message), loop
            )
        except RuntimeError:
            logger.debug("Broadcast dropped: server loop unavailable")
            return
        future.add_done_callback(_log_broadcast_failure)

    def schedule_invalidate_connections(self) -> None:
        """Close all current sockets shortly after token regeneration."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._invalidate_connections(), loop
            )
        except RuntimeError:
            logger.debug("Socket invalidation dropped: server loop unavailable")
            return
        future.add_done_callback(_log_broadcast_failure)

    async def _invalidate_connections(self) -> None:
        # Give the regenerating REST response time to deliver the new host URL.
        await asyncio.sleep(0.25)
        sockets = list(self._connections)
        self._connections.clear()
        await asyncio.gather(
            *(self._close(ws, WS_CLOSE_UNAUTHORIZED) for ws in sockets),
            return_exceptions=True,
        )

    async def broadcast_json(self, message: Dict[str, Any]) -> None:
        """Send ``message`` to every connected client.

        Sockets still assembling their ``hello`` buffer the payload instead;
        live sockets are written concurrently so one stalled peer cannot
        block the fan-out.
        """
        if not self._connections:
            return
        text = json.dumps(message, ensure_ascii=False, default=str)
        live: List[WebSocket] = []
        for websocket, conn in list(self._connections.items()):
            if conn.ready:
                live.append(websocket)
            elif conn.overflowed:
                continue
            elif len(conn.pending) >= MAX_PENDING_MESSAGES:
                conn.overflowed = True
                conn.pending.clear()
            else:
                conn.pending.append(text)
        if live:
            await asyncio.gather(
                *(self._send_guarded(websocket, text) for websocket in live)
            )

    async def _send_guarded(self, websocket: WebSocket, text: str) -> None:
        """Broadcast one payload to one socket, dropping it when it stalls."""
        conn = self._connections.get(websocket)
        if conn is None:
            return
        try:
            await asyncio.wait_for(
                websocket.send_text(text), BROADCAST_SEND_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            conn.send_timeouts += 1
            logger.warning("Dashboard socket stalled (%d consecutive timeouts)",
                           conn.send_timeouts)
            if conn.send_timeouts >= MAX_SEND_TIMEOUTS:
                await self._drop(websocket, "send timed out repeatedly")
        except Exception:
            await self._drop(websocket, "send failed")
        else:
            conn.send_timeouts = 0

    async def _drop(self, websocket: WebSocket, reason: str) -> None:
        """Unregister and close a socket that can no longer be served."""
        self._connections.pop(websocket, None)
        logger.info("Dropping dashboard socket: %s", reason)
        await self._close(websocket, 1011)

    async def _send(self, websocket: WebSocket, message: Dict[str, Any]) -> None:
        await websocket.send_text(
            json.dumps(message, ensure_ascii=False, default=str)
        )

    async def _close(self, websocket: WebSocket, code: int) -> None:
        """Close a socket, tolerating a peer that already went away."""
        try:
            await websocket.close(code=code)
        except Exception:
            logger.debug("WebSocket close failed", exc_info=True)

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    async def handle_connection(self, websocket: WebSocket) -> None:
        """Serve one dashboard WebSocket from handshake to disconnect.

        Validates the token, registers the socket (buffering broadcasts),
        joins guests as participants, sends the ``hello`` snapshot, flushes
        anything buffered meanwhile, then runs the receive loop.
        """
        await websocket.accept()
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        self._ensure_store_subscription()

        engine = self._engine
        token = websocket.query_params.get("token", "")
        name = (websocket.query_params.get("name") or "").strip()
        guest_id = _clean_guest_id(websocket.query_params.get("guest_id"))

        meeting_id = getattr(engine, "meeting_id", None)
        store = getattr(engine, "store", None)
        meeting: Optional[Dict[str, Any]] = None
        if meeting_id:
            meeting = await asyncio.to_thread(
                self._repository.get_meeting, meeting_id
            )
        if meeting is None or store is None:
            await self._close(websocket, WS_CLOSE_UNAUTHORIZED)
            return

        role = resolve_role(
            token, meeting.get("host_token"), meeting.get("guest_token")
        )
        if role is None:
            await self._close(websocket, WS_CLOSE_UNAUTHORIZED)
            return
        if role == "guest" and not name:
            await self._close(websocket, WS_CLOSE_NAME_REQUIRED)
            return
        if len(self._connections) >= MAX_CONNECTIONS:
            logger.warning("Refusing dashboard connection: %d already served",
                           len(self._connections))
            await self._close(websocket, WS_CLOSE_TOO_MANY)
            return

        # Registered before any further I/O: every patch raised while the
        # snapshot and segment reads are in flight lands in ``pending``
        # instead of vanishing between the snapshot and the first live patch.
        conn = _Connection(role, name)
        self._connections[websocket] = conn

        participant: Optional[Dict[str, Any]] = None
        try:
            if role == "guest":
                participant = await asyncio.to_thread(
                    self._join_guest, guest_id, name
                )
                conn.participant_id = (participant or {}).get("id")
            else:
                conn.participant_id = await asyncio.to_thread(
                    self._host_participant_id
                )
            await self._send_hello(websocket, conn, store, meeting, meeting_id)
        except Exception:
            logger.exception("Dashboard handshake failed (meeting %s)", meeting_id)
            self._connections.pop(websocket, None)
            await self._close(websocket, 1011)
            return

        if not await self._flush_pending(websocket, conn):
            return

        if role == "guest" and participant is not None:
            await self.broadcast_json({
                "type": "presence", "event": "joined",
                "participant": participant,
            })

        try:
            await self._receive_loop(websocket, conn)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("Dashboard connection failed (meeting %s)",
                             meeting_id)
        finally:
            self._connections.pop(websocket, None)
            if role == "guest" and participant is not None:
                await self._announce_left(conn, participant)
            await self._close(websocket, 1000)

    async def _send_hello(self, websocket: WebSocket, conn: _Connection,
                          store: Any, meeting: Dict[str, Any],
                          meeting_id: str) -> None:
        """Assemble and send the ``hello`` snapshot off the event loop."""
        state = await asyncio.to_thread(store.snapshot)
        try:
            segments = await asyncio.to_thread(
                self._repository.get_last_segments, meeting_id,
                HELLO_SEGMENT_COUNT,
            )
        except Exception:
            logger.exception("Failed to load hello segments")
            segments = []
        guest_url = await asyncio.to_thread(self._guest_url, meeting)

        await self._send(websocket, {
            "type": "hello",
            "role": conn.role,
            "participant_id": conn.participant_id,
            "seq": state.get("seq", 0),
            "state": state,
            "segments": segments,
            "urls": {"guest": guest_url},
            "meeting": {
                "id": meeting.get("id"),
                "title": state.get("title") or meeting.get("title") or "",
                "started_at": meeting.get("started_at"),
                "status": state.get("status") or meeting.get("status"),
            },
        })

    async def _flush_pending(self, websocket: WebSocket,
                             conn: _Connection) -> bool:
        """Replay broadcasts buffered during ``hello``, then go live.

        Returns:
            True when the socket is ready to serve; False when it was dropped
            (buffer overflow or a failed write) and the caller must stop.
        """
        if conn.overflowed:
            logger.warning("Pre-hello buffer overflowed; asking client to resync")
            self._connections.pop(websocket, None)
            await self._close(websocket, WS_CLOSE_RESYNC)
            return False
        while conn.pending:
            text = conn.pending.popleft()
            try:
                await websocket.send_text(text)
            except Exception:
                logger.debug("Buffered broadcast failed", exc_info=True)
                self._connections.pop(websocket, None)
                await self._close(websocket, 1011)
                return False
        # No await between the drain and the flag, so nothing can slip in.
        conn.ready = True
        return True

    async def _announce_left(self, conn: _Connection,
                             participant: Dict[str, Any]) -> None:
        """Broadcast a guest's departure with its freshest participant data."""
        try:
            current = await asyncio.to_thread(
                self._participant_snapshot, conn.participant_id
            )
            await self.broadcast_json({
                "type": "presence", "event": "left",
                "participant": current or participant,
            })
        except Exception:
            logger.debug("Presence 'left' broadcast failed", exc_info=True)

    def _join_guest(self, guest_id: Optional[str],
                    name: str) -> Optional[Dict[str, Any]]:
        """Resolve a joining guest to a participant (blocking; run in a thread).

        Reuses the participant this ``guest_id`` joined with previously, so a
        reconnect storm cannot mint an unbounded number of participants. A
        changed display name (or an unknown key) creates a new participant.

        Args:
            guest_id: The client's stable guest key, when supplied.
            name: The display name presented on this connection.

        Returns:
            The participant dict for this guest.
        """
        if guest_id:
            known = self._guest_participants.get(guest_id)
            if known and known[1] == name:
                existing = self._participant_snapshot(known[0])
                if existing is not None:
                    return existing
        participant = self._engine.add_guest(name)
        participant_id = (participant or {}).get("id")
        if guest_id and participant_id:
            self._guest_participants[guest_id] = (participant_id, name)
        return participant

    def _host_participant_id(self) -> Optional[str]:
        """The host's ``kind == 'me'`` participant id (blocking; run in a thread)."""
        store = getattr(self._engine, "store", None)
        if store is None:
            return None
        for pid, pdata in (store.snapshot().get("participants") or {}).items():
            if pdata.get("kind") == "me":
                return pid
        return None

    def _guest_url(self, meeting: Dict[str, Any]) -> str:
        """Absolute guest URL when the server provided one, else relative.

        Blocking: the provider reads the meeting row, so callers on the event
        loop must route this through a worker thread.
        """
        if self.get_guest_url is not None:
            try:
                url = self.get_guest_url()
                if url:
                    return url
            except Exception:
                logger.exception("guest_url provider raised")
        return f"/m/{meeting.get('guest_token', '')}"

    def _participant_snapshot(
        self, participant_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Current participant dict from the live state, if available.

        Blocking: takes a store snapshot, so callers on the event loop must
        route this through a worker thread.
        """
        if not participant_id:
            return None
        store = getattr(self._engine, "store", None)
        if store is None:
            return None
        try:
            return (store.snapshot().get("participants") or {}).get(participant_id)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self, websocket: WebSocket,
                            conn: _Connection) -> None:
        """Read and dispatch client messages until the socket disconnects.

        Every frame is validated before parsing and every handler failure is
        answered with an ``error`` message, so no client — guest or host —
        can abort the loop with malformed input.
        """
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            raw = message.get("text")
            if raw is None:
                await self._send(websocket, {
                    "type": "error", "code": "binary_unsupported",
                    "message": "Binary frames are not supported.",
                })
                continue
            if len(raw) > MAX_MESSAGE_CHARS:
                await self._send(websocket, {
                    "type": "error", "code": "message_too_large",
                    "message": "Message exceeds the size limit.",
                })
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                await self._send(websocket, {
                    "type": "error", "code": "bad_json",
                    "message": "Message was not valid JSON.",
                })
                continue
            if not isinstance(parsed, dict):
                await self._send(websocket, {
                    "type": "error", "code": "bad_message",
                    "message": "Message must be a JSON object.",
                })
                continue
            try:
                await self._dispatch(websocket, conn, parsed)
            except WebSocketDisconnect:
                raise
            except Exception:
                logger.exception("Dashboard message handling failed")
                await self._send(websocket, {
                    "type": "error", "code": "internal_error",
                    "message": "The server could not process that message.",
                })

    async def _dispatch(self, websocket: WebSocket, conn: _Connection,
                        message: Dict[str, Any]) -> None:
        """Route one well-formed client message to its handler."""
        msg_type = message.get("type")
        if msg_type == "ping":
            await self._send(websocket, {"type": "pong"})
        elif msg_type == "action":
            await self._handle_action(websocket, conn, message)
        elif msg_type == "undo":
            await self._handle_undo(websocket, conn, message)
        else:
            await self._send(websocket, {
                "type": "error", "code": "unknown_type",
                "message": f"Unknown message type: {msg_type!r}",
            })

    async def _handle_action(self, websocket: WebSocket, conn: _Connection,
                             message: Dict[str, Any]) -> None:
        """Apply one client op and echo its ``action_result``."""
        op = message.get("op")
        if not isinstance(op, dict):
            results = _rejection("malformed_op")
        elif conn.inflight >= MAX_INFLIGHT_ACTIONS:
            results = _rejection("too_many_in_flight")
        else:
            actor_type = "host" if conn.role == "host" else "user"
            conn.inflight += 1
            try:
                applied = await asyncio.to_thread(
                    self._engine.apply_client_action,
                    actor_type, conn.participant_id, op,
                )
                results = [_op_result_dict(r) for r in applied]
            except Exception:
                logger.exception("apply_client_action failed")
                results = _rejection("internal_error")
            finally:
                conn.inflight -= 1
        await self._send(websocket, {
            "type": "action_result",
            "client_action_id": message.get("client_action_id"),
            "results": results,
        })

    async def _handle_undo(self, websocket: WebSocket, conn: _Connection,
                           message: Dict[str, Any]) -> None:
        """Apply a host undo of a past event and echo its ``action_result``."""
        if conn.role != "host":
            results = _rejection("host_only")
        else:
            seq = _coerce_seq(message.get("seq"))
            if seq is None:
                results = _rejection("invalid_seq")
            elif conn.inflight >= MAX_INFLIGHT_ACTIONS:
                results = _rejection("too_many_in_flight")
            else:
                conn.inflight += 1
                try:
                    applied = await asyncio.to_thread(
                        self._engine.undo, seq, conn.participant_id
                    )
                    results = [_op_result_dict(r) for r in applied]
                except Exception:
                    logger.exception("undo failed")
                    results = _rejection("internal_error")
                finally:
                    conn.inflight -= 1
        await self._send(websocket, {
            "type": "action_result",
            "client_action_id": message.get("client_action_id"),
            "results": results,
        })
