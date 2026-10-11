"""Live API routes on the in-app listener: state, events, actions, recording.

Contract: docs/design/automation/design.md, "services/automation/api.py".
Mounted by services/agent_mcp/app.py only; the headless ``--api`` never
serves these routes because there is no running app behind it. Requests
arrive already authenticated by the app's bearer-token middleware.

Every request rereads the user's permissions (Settings > API & MCP). Calls
into the app go through ``AppControl``, which can block for about two
seconds, so they run on a worker thread and never on the event loop.

``GET /v1/events`` is a Server-Sent Events stream. Each event is::

    id: <seq>
    event: <type>
    data: <the event's JSON: id, seq, type, time, v, data>

``seq`` counts up through one app session; reconnecting with
``Last-Event-ID`` (or ``?since=<seq>``) replays what the stream missed while
the app still holds it. When it no longer does, the stream first sends a
synthetic ``events.gap`` event (no ``id:`` line, ``seq`` 0) whose
``data.missed_from_seq`` is the first event that may be lost; fetch history
to catch up. A client that reads too slowly gets an ``events.gap`` and the
stream ends, so reconnecting with ``Last-Event-ID`` resumes it. A ``: ping``
comment arrives every 15 seconds.
"""

# No ``from __future__ import annotations``: FastAPI reads the route
# signatures, whose types are imported inside create_live_router.
import asyncio
import json
import logging
import secrets
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

#: ``bus().subscribers(label)`` names for the two kinds of stream.
SSE_LABEL = "sse"
CHANNEL_LABEL = "claude-channel"
#: A stream whose request has ``X-OpenWhisper-Client: claude-channel`` is a
#: Claude Code channel listener.
CLIENT_HEADER = "X-OpenWhisper-Client"
CHANNEL_CLIENT = "claude-channel"

MAX_ACTION_TEXT = 20_000
MAX_NOTICE_TEXT = 500
MAX_NOTICE_TITLE = 100
MAX_ID_CHARS = 64
MAX_URL_CHARS = 2048
#: Request bodies, in bytes: room for the longest text even when every
#: character arrives as a JSON escape.
MAX_RUN_BODY = 256 * 1024
MAX_SMALL_BODY = 16 * 1024

PING_INTERVAL_S = 15.0
#: How often a stream notices that MCP was switched off.
ENABLED_CHECK_S = 0.5
#: How often a stream rereads the live events permission.
PERMISSION_CHECK_S = 2.0
#: Events waiting for one slow client; past this the stream ends with a gap.
STREAM_QUEUE_SIZE = 1000

#: ``ControlError.code`` to HTTP status.
_CONTROL_STATUS = {"busy": 409, "not_found": 404, "refused": 422, "unavailable": 503}


class _Invalid(Exception):
    """A request the API cannot accept; ``message`` is shown to the caller."""

    def __init__(self, message: str, status: int = 422, code: str = "invalid_parameters") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def parse_types(raw: Optional[str]) -> Optional[frozenset]:
    """``?types=a,b`` as a set of event types; None for every type.

    Raises:
        _Invalid: A name is not an event type.
    """
    from services.automation import catalog

    if raw is None:
        return None
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if not names:
        return None
    unknown = [name for name in names if name not in catalog.EVENT_TYPES]
    if unknown:
        raise _Invalid("types names an event type OpenWhisper doesn't publish.")
    return frozenset(names)


def parse_last_event_id(raw: Optional[str]) -> Optional[int]:
    """The ``Last-Event-ID`` header as a sequence number; None when absent."""
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if not value.isdigit() or len(value) > 18:
        raise _Invalid("Last-Event-ID must be the id of an event from this stream.")
    return int(value)


#: Characters str.splitlines() breaks on that JSON leaves unescaped. Readers
#: built on it (httpx's iter_lines) would cut an event's data line in two;
#: as JSON escapes they decode to the same text.
_LINE_BREAKS = str.maketrans({" ": "\\u2028", " ": "\\u2029", "\u0085": "\\u0085"})


def sse_frame(event, *, with_id: bool = True) -> bytes:
    """One Server-Sent Events message for ``event`` (an ``events.Event``)."""
    lines = [f"id: {event.seq}"] if with_id else []
    lines.append(f"event: {event.type}")
    # Compact JSON escapes CR and LF, so one data line is the whole event.
    lines.append(f"data: {event.to_json().translate(_LINE_BREAKS)}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def gap_event(missed_from_seq: int):
    """The synthetic ``events.gap`` event; it never goes through the bus."""
    from services.automation import catalog
    from services.automation.events import Event

    return Event(
        id="evt_" + secrets.token_hex(12),
        seq=0,
        type=catalog.EVENTS_GAP,
        time=_now_iso(),
        data=catalog.events_gap(missed_from_seq),
    )


def _log_failure(route: str, exc: BaseException) -> None:
    """Report an unexpected failure by type and place, never by its message.

    Messages can quote the request's text or a URL, which must not reach logs.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    where = f"{frames[-1].filename}:{frames[-1].lineno}" if frames else "?"
    logger.error("Live API %s failed: %s at %s", route, type(exc).__name__, where)


async def read_json_body(request, limit: int) -> dict:
    """The request's JSON object, read with a size limit; {} for an empty body.

    Raises:
        _Invalid: Too large (413), not JSON, or not an object.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _Invalid("The request body is too large.", 413, "request_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise _Invalid("The request body is too large.", 413, "request_too_large")
    if not body.strip():
        return {}
    try:
        value = json.loads(bytes(body))
    except (UnicodeDecodeError, ValueError):
        raise _Invalid("The request body must be a JSON object.") from None
    if not isinstance(value, dict):
        raise _Invalid("The request body must be a JSON object.")
    return value


def _string(body: Mapping[str, Any], key: str, *, required: bool, max_chars: int,
            label: str = "") -> str:
    value = body.get(key)
    label = label or key
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value) or len(value) > max_chars:
        if required:
            raise _Invalid(f"{label} must be text of 1 to {max_chars:,} characters.")
        raise _Invalid(f"{label} must be text of at most {max_chars:,} characters.")
    return value


def _lease_dict(lease) -> dict:
    from services.automation import filters

    return {
        "id": lease.id,
        "url": lease.url,
        "stage": lease.stage,
        "timeout_ms": lease.timeout_ms,
        "modes": list(lease.modes),
        "expires_at": lease.expires_at,
        "ttl_s": filters.LEASE_TTL_S,
    }


class EventStream:
    """One client's ``/v1/events``: replay, then live events, until it ends.

    The bus calls ``_deliver`` on its dispatch thread; that only hands the
    event to this stream's event loop, which keeps it in a bounded queue.
    """

    def __init__(self, *, types: Optional[Iterable[str]], replay_from: int,
                 label: str, enabled: Callable[[], bool],
                 allowed: Callable[[], bool]) -> None:
        self.types = frozenset(types) if types is not None else None
        self.replay_from = replay_from
        self.label = label
        self.enabled = enabled
        self.allowed = allowed
        self._pending: deque = deque()
        # The first event that found the queue full; 0 while none has.
        self._overflow_seq = 0
        self._ready: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscription = None

    # -- the bus side (dispatch thread) ---------------------------------------

    def _deliver(self, event) -> None:
        loop = self._loop
        try:
            loop.call_soon_threadsafe(self._enqueue, event)
        except RuntimeError:
            # The server's loop is gone; nothing will read this stream again.
            subscription = self._subscription
            if subscription is not None:
                subscription.close()

    # -- the event loop side ----------------------------------------------------

    def _enqueue(self, event) -> None:
        if self._overflow_seq:
            return
        if len(self._pending) >= STREAM_QUEUE_SIZE:
            self._overflow_seq = event.seq
        else:
            self._pending.append(event)
        self._ready.set()

    async def body(self):
        """The response body: bytes of SSE messages."""
        from services.automation.events import bus

        loop = self._loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()
        events_bus = bus()
        # Subscribe before reading the buffer, so nothing falls between the
        # replay and the live events; duplicates are dropped by seq below.
        self._subscription = events_bus.subscribe(
            self._deliver, types=self.types, label=self.label
        )
        try:
            yield b"retry: 2000\n\n"
            last = events_bus.last_seq()
            replay, gap = events_bus.since(self.replay_from, types=self.types)
            if gap:
                # A position past the newest event is from an earlier app
                # session; this one's numbering started again at 1.
                missed = 1 if self.replay_from > last else self.replay_from + 1
                yield sse_frame(gap_event(missed), with_id=False)
            replayed = set()
            for event in replay:
                replayed.add(event.seq)
                yield sse_frame(event)

            next_ping = loop.time() + PING_INTERVAL_S
            next_permission = loop.time() + PERMISSION_CHECK_S
            while self.enabled():
                now = loop.time()
                if now >= next_permission:
                    next_permission = now + PERMISSION_CHECK_S
                    if not await asyncio.to_thread(self.allowed):
                        return
                if self._overflow_seq:
                    # The client fell too far behind: say where it lost track
                    # and end, so it reconnects with Last-Event-ID and replays.
                    missed = next(
                        (e.seq for e in self._pending if e.seq not in replayed),
                        self._overflow_seq,
                    )
                    self._pending.clear()
                    yield sse_frame(gap_event(missed), with_id=False)
                    return
                if self._pending:
                    event = self._pending.popleft()
                    if event.seq not in replayed:
                        yield sse_frame(event)
                    continue
                if now >= next_ping:
                    next_ping = now + PING_INTERVAL_S
                    yield b": ping\n\n"
                    continue
                self._ready.clear()
                wait = min(next_ping, next_permission, now + ENABLED_CHECK_S) - now
                try:
                    await asyncio.wait_for(self._ready.wait(), timeout=max(0.0, wait))
                except asyncio.TimeoutError:
                    pass
        finally:
            self._subscription.close()


def create_live_router(settings, *, enabled: Optional[Callable[[], bool]] = None):
    """The live routes.

    Args:
        settings: The app's SettingsManager; None means default settings
            (live events on, every other permission off).
        enabled: False once MCP is switched off or the listener is stopping;
            open event streams end within half a second.
    """
    from typing import Annotated

    from fastapi import APIRouter, Header, Path, Query, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    from services.agent_api.app import _error
    from services.automation import filters, permissions
    from services.automation.control import ControlError, require_app_control
    from services.automation.events import bus

    router = APIRouter()
    is_enabled = enabled if enabled is not None else (lambda: True)
    Id = Annotated[str, Path(min_length=1, max_length=MAX_ID_CHARS)]

    def saved() -> Mapping[str, Any]:
        return settings.load_all_settings() if settings is not None else {}

    def failure(route: str, exc: BaseException):
        if isinstance(exc, permissions.PermissionDenied):
            return _error(403, "permission_denied", exc.message)
        if isinstance(exc, ControlError):
            return _error(_CONTROL_STATUS.get(exc.code, 503), exc.code, exc.message)
        if isinstance(exc, _Invalid):
            return _error(exc.status, exc.code, exc.message)
        _log_failure(route, exc)
        return _error(503, "unavailable", "OpenWhisper couldn't do that right now. Try again.")

    async def respond(route: str, work: Callable[[], Any], *, status: int = 202):
        """Run ``work`` on a worker thread and answer with its JSON result."""
        try:
            result = await asyncio.to_thread(work)
        except Exception as exc:
            return failure(route, exc)
        if result is None:
            return Response(status_code=status)
        return JSONResponse(result, status_code=status)

    @router.get("/v1/live/state", operation_id="get_live_state")
    async def live_state():
        """Whether OpenWhisper is recording or busy, its permissions and listeners."""

        def work():
            current = require_app_control().state()
            events_bus = bus()
            return {
                **current.to_dict(),
                "permissions": permissions.describe(saved()),
                "listeners": {
                    "events": events_bus.subscribers(SSE_LABEL),
                    "claude_channel": events_bus.subscribers(CHANNEL_LABEL),
                },
            }

        return await respond("state", work, status=200)

    @router.get("/v1/events", operation_id="stream_events")
    async def stream_events(
        types: Annotated[Optional[str], Query(max_length=2000)] = None,
        since: Annotated[Optional[int], Query(ge=0)] = None,
        last_event_id: Annotated[Optional[str], Header(max_length=64)] = None,
        x_openwhisper_client: Annotated[Optional[str], Header(max_length=64)] = None,
    ):
        """Live events as Server-Sent Events; see this module's docstring."""
        try:
            wanted = parse_types(types)
            resume = parse_last_event_id(last_event_id)
            await asyncio.to_thread(lambda: permissions.require(saved(), permissions.LIVE_EVENTS))
        except Exception as exc:
            return failure("events", exc)
        # EventSource reconnects to the same URL, so the header is the newer
        # position. Without either, the stream starts at this request.
        if resume is None:
            resume = since if since is not None else bus().last_seq()
        label = CHANNEL_LABEL if (x_openwhisper_client or "").strip() == CHANNEL_CLIENT else SSE_LABEL
        stream = EventStream(
            types=wanted,
            replay_from=resume,
            label=label,
            enabled=is_enabled,
            allowed=lambda: permissions.granted(saved(), permissions.LIVE_EVENTS),
        )
        return StreamingResponse(
            stream.body(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @router.get("/v1/actions", operation_id="list_actions")
    async def list_actions():
        """The user's enabled actions: id, name, kind, description, spoken names."""
        return await respond(
            "actions", lambda: {"actions": require_app_control().list_actions()}, status=200
        )

    @router.post("/v1/actions/{action_id}/run", status_code=202, operation_id="run_action")
    async def run_action(action_id: Id, request: Request):
        """Run an action on text, as if the user had said it. Body: ``{"text"}``."""
        try:
            body = await read_json_body(request, MAX_RUN_BODY)
        except _Invalid as exc:
            return failure("run", exc)

        def work():
            permissions.require(saved(), permissions.RUN_ACTIONS)
            text = _string(body, "text", required=True, max_chars=MAX_ACTION_TEXT)
            return {"job_id": require_app_control().run_action(action_id, text, source="api")}

        return await respond("run", work)

    @router.post("/v1/recording/start", status_code=202, operation_id="start_recording")
    async def start_recording(request: Request):
        """Start recording a dictation, or words for ``{"action_id"}``."""
        try:
            body = await read_json_body(request, MAX_SMALL_BODY)
        except _Invalid as exc:
            return failure("start", exc)

        def work():
            permissions.require(saved(), permissions.CONTROL_RECORDING)
            action_id = _string(body, "action_id", required=False, max_chars=MAX_ID_CHARS)
            job_id = require_app_control().start_recording(action_id=action_id, source="api")
            return {"job_id": job_id}

        return await respond("start", work)

    @router.post("/v1/recording/stop", status_code=202, operation_id="stop_recording")
    async def stop_recording():
        """Stop the recording; its words are transcribed and delivered as usual."""

        def work():
            permissions.require(saved(), permissions.CONTROL_RECORDING)
            require_app_control().stop_recording()
            return {}

        return await respond("stop", work)

    @router.post("/v1/recording/cancel", status_code=202, operation_id="cancel_recording")
    async def cancel_recording():
        """Throw away the recording or the transcription in progress."""

        def work():
            permissions.require(saved(), permissions.CONTROL_RECORDING)
            require_app_control().cancel()
            return {}

        return await respond("cancel", work)

    @router.post("/v1/filters", status_code=201, operation_id="create_filter_lease")
    async def create_filter(request: Request):
        """Lease a text filter: ``{"url", "stage", "timeout_ms"?}``; renew within 60 s."""
        try:
            body = await read_json_body(request, MAX_SMALL_BODY)
        except _Invalid as exc:
            return failure("filters", exc)

        def work():
            permissions.require(saved(), permissions.TEXT_FILTERS)
            url = _string(body, "url", required=True, max_chars=MAX_URL_CHARS)
            stage = _string(body, "stage", required=True, max_chars=MAX_ID_CHARS)
            options = {}
            if body.get("timeout_ms") is not None:
                timeout_ms = body["timeout_ms"]
                if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
                    raise _Invalid("timeout_ms must be a whole number of milliseconds.")
                options["timeout_ms"] = timeout_ms
            try:
                lease = filters.register_lease(url, stage, **options)
            except ValueError as exc:
                raise _Invalid(str(exc)) from None
            return _lease_dict(lease)

        return await respond("filters", work, status=201)

    @router.post("/v1/filters/{lease_id}/renew", operation_id="renew_filter_lease")
    async def renew_filter(lease_id: Id):
        """Keep a leased text filter for another 60 seconds."""

        def work():
            permissions.require(saved(), permissions.TEXT_FILTERS)
            try:
                return _lease_dict(filters.renew_lease(lease_id))
            except KeyError:
                raise ControlError("not_found", "That filter lease has expired. Create a new one.") from None

        return await respond("filters", work, status=200)

    @router.delete("/v1/filters/{lease_id}", status_code=204, operation_id="delete_filter_lease")
    async def delete_filter(lease_id: Id):
        """Stop a leased text filter now."""

        def work():
            permissions.require(saved(), permissions.TEXT_FILTERS)
            if not filters.drop_lease(lease_id):
                raise ControlError("not_found", "That filter lease has expired.")
            return None

        return await respond("filters", work, status=204)

    @router.post("/v1/notify", status_code=202, operation_id="notify_user")
    async def notify(request: Request):
        """Show a short message near the pointer: ``{"text", "title"?}``."""
        try:
            body = await read_json_body(request, MAX_SMALL_BODY)
        except _Invalid as exc:
            return failure("notify", exc)

        def work():
            permissions.require(saved(), permissions.RUN_ACTIONS)
            text = _string(body, "text", required=True, max_chars=MAX_NOTICE_TEXT)
            title = _string(body, "title", required=False, max_chars=MAX_NOTICE_TITLE)
            if not text.strip():
                raise _Invalid(f"text must be text of 1 to {MAX_NOTICE_TEXT:,} characters.")
            require_app_control().notify(text, title=title)
            return {}

        return await respond("notify", work)

    return router
