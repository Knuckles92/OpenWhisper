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

``seq`` keeps counting up, across app restarts too; reconnecting with
``Last-Event-ID`` (or ``?since=<seq>``, 0 for everything since the app
started) replays what the stream missed while the app still holds it. When
the stream may have missed some, it first sends a synthetic ``events.gap``
event (no ``id:`` line, ``seq`` 0) whose ``data.missed_from_seq`` is the
first event that may be lost and ``data.reason`` says why: ``evicted`` (the
app no longer holds them; fetch history to catch up) or ``restarted`` (the
position is from before the app restarted; everything since follows). A
client that reads too slowly gets an ``events.gap`` with reason ``slow`` and
the stream ends, so reconnecting with ``Last-Event-ID`` resumes it. A
``: ping`` comment arrives every 15 seconds.
"""

# No ``from __future__ import annotations``: FastAPI reads the route
# signatures, whose types are imported inside the _register_*_routes functions.
import asyncio
import ipaddress
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


def sse_frame(event, *, with_id: bool = True) -> bytes:
    """One Server-Sent Events message for ``event`` (an ``events.Event``)."""
    lines = [f"id: {event.seq}"] if with_id else []
    lines.append(f"event: {event.type}")
    # ASCII JSON: one data line whatever the text holds (every line break,
    # U+2028 included, is an escape), and nothing that can't be encoded.
    data = json.dumps(event.to_dict(), ensure_ascii=True, separators=(",", ":"))
    lines.append(f"data: {data}")
    return ("\n".join(lines) + "\n\n").encode("ascii")


def gap_event(missed_from_seq: int, reason: str):
    """The synthetic ``events.gap`` event; it never goes through the bus."""
    from services.automation import catalog
    from services.automation.events import Event

    return Event(
        id="evt_" + secrets.token_hex(12),
        seq=0,
        type=catalog.EVENTS_GAP,
        time=_now_iso(),
        data=catalog.events_gap(missed_from_seq, reason),
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


def peer_is_local(request) -> bool:
    """Whether ``request`` (a Starlette request) came from this computer.

    The listener answers loopback clients and, when the user allows agents
    over Tailscale, Tailscale peers; anything but a loopback address is
    another computer.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") or ""
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


def is_unicode_text(value: str) -> bool:
    """False for a string with a lone UTF-16 surrogate (half an emoji).

    JSON can carry one as an escape, but it is not text: it can't be encoded
    as UTF-8, so it must never get into a job, an event or a notice.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


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
    if not is_unicode_text(value):
        raise _Invalid(f"{label} must be valid Unicode text.")
    return value


def _json_body(properties: Mapping[str, dict], *required: str) -> dict:
    """``openapi_extra`` describing a body that ``read_json_body`` reads."""
    schema = {"type": "object", "properties": dict(properties), "required": list(required)}
    return {"requestBody": {
        "required": bool(required),
        "content": {"application/json": {"schema": schema}},
    }}


def _text_field(max_chars: int, description: str) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": max_chars, "description": description}


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

    def _replay(self, events_bus) -> tuple:
        """The frames that catch this stream up, and the position they leave it at."""
        last = events_bus.last_seq()
        replay, gap = events_bus.since(self.replay_from, types=self.types)
        from_other_session = self.replay_from > last
        frames = []
        if gap:
            # A position past the newest event is from another app
            # session, whose numbering this one doesn't continue.
            missed = 1 if from_other_session else self.replay_from + 1
            frames.append(sse_frame(gap_event(missed, gap), with_id=False))
        # The stream's position only rises: a live event at or below it
        # was replayed, or came before the stream's start (the bus's
        # dispatch can lag behind publishing), and sending it now would
        # repeat it or put it after newer ones.
        position = 0 if from_other_session else self.replay_from
        for event in replay:
            position = max(position, event.seq)
            frames.append(sse_frame(event))
        return frames, position

    def _slow_gap_frame(self, position: int) -> bytes:
        """The ``events.gap`` that ends a stream whose client fell too far behind."""
        from services.automation.catalog import GAP_SLOW

        missed = next(
            (e.seq for e in self._pending if e.seq > position),
            self._overflow_seq,
        )
        self._pending.clear()
        return sse_frame(gap_event(missed, GAP_SLOW), with_id=False)

    async def _wait_for_events(self, now: float, next_ping: float, next_permission: float) -> None:
        """Sleep until an event arrives or the next ping, permission or enabled check."""
        self._ready.clear()
        wait = min(next_ping, next_permission, now + ENABLED_CHECK_S) - now
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=max(0.0, wait))
        except asyncio.TimeoutError:
            pass

    async def body(self):
        """The response body: bytes of SSE messages."""
        from services.automation.events import bus

        loop = self._loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()
        events_bus = bus()
        # Subscribe before reading the buffer, so nothing falls between the
        # replay and the live events.
        self._subscription = events_bus.subscribe(
            self._deliver, types=self.types, label=self.label
        )
        try:
            yield b"retry: 2000\n\n"
            frames, position = self._replay(events_bus)
            for frame in frames:
                yield frame

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
                    yield self._slow_gap_frame(position)
                    return
                if self._pending:
                    event = self._pending.popleft()
                    if event.seq > position:
                        position = event.seq
                        yield sse_frame(event)
                    continue
                if now >= next_ping:
                    next_ping = now + PING_INTERVAL_S
                    yield b": ping\n\n"
                    continue
                await self._wait_for_events(now, next_ping, next_permission)
        finally:
            self._subscription.close()


class _LiveRoutes:
    """What every live route shares: the saved settings and how failures answer."""

    def __init__(self, settings, enabled: Optional[Callable[[], bool]]) -> None:
        self.settings = settings
        self.is_enabled = enabled if enabled is not None else (lambda: True)

    def saved(self) -> Mapping[str, Any]:
        return self.settings.load_all_settings() if self.settings is not None else {}

    def failure(self, route: str, exc: BaseException):
        from services.agent_api.app import _error
        from services.automation import permissions
        from services.automation.control import ControlError

        if isinstance(exc, permissions.PermissionDenied):
            return _error(403, "permission_denied", exc.message)
        if isinstance(exc, ControlError):
            return _error(_CONTROL_STATUS.get(exc.code, 503), exc.code, exc.message)
        if isinstance(exc, _Invalid):
            return _error(exc.status, exc.code, exc.message)
        _log_failure(route, exc)
        return _error(503, "unavailable", "OpenWhisper couldn't do that right now. Try again.")

    async def respond(self, route: str, work: Callable[[], Any], *, status: int = 202):
        """Run ``work`` on a worker thread and answer with its JSON result."""
        from fastapi.responses import JSONResponse, Response

        try:
            result = await asyncio.to_thread(work)
        except Exception as exc:
            return self.failure(route, exc)
        if result is None:
            return Response(status_code=status)
        return JSONResponse(result, status_code=status)


def _id_param():
    """A path id, as every live route with one takes it."""
    from typing import Annotated

    from fastapi import Path

    return Annotated[str, Path(min_length=1, max_length=MAX_ID_CHARS)]


def _stream_label(client: Optional[str]) -> str:
    return CHANNEL_LABEL if (client or "").strip() == CHANNEL_CLIENT else SSE_LABEL


def _register_state_routes(router, live: _LiveRoutes) -> None:
    from typing import Annotated

    from fastapi import Header, Query
    from fastapi.responses import StreamingResponse

    from services.automation import permissions
    from services.automation.control import require_app_control
    from services.automation.events import bus

    @router.get("/v1/live/state", operation_id="get_live_state")
    async def live_state():
        """Whether OpenWhisper is recording or busy, its permissions and listeners."""

        def work():
            current = require_app_control().state()
            events_bus = bus()
            return {
                **current.to_dict(),
                "permissions": permissions.describe(live.saved()),
                "listeners": {
                    "events": events_bus.subscribers(SSE_LABEL),
                    "claude_channel": events_bus.subscribers(CHANNEL_LABEL),
                },
            }

        return await live.respond("state", work, status=200)

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
            await asyncio.to_thread(lambda: permissions.require(live.saved(), permissions.LIVE_EVENTS))
        except Exception as exc:
            return live.failure("events", exc)
        # EventSource reconnects to the same URL, so the header is the newer
        # position. Without either, the stream starts at this request.
        if resume is None:
            resume = since if since is not None else bus().last_seq()
        stream = EventStream(
            types=wanted,
            replay_from=resume,
            label=_stream_label(x_openwhisper_client),
            enabled=live.is_enabled,
            allowed=lambda: permissions.granted(live.saved(), permissions.LIVE_EVENTS),
        )
        return StreamingResponse(
            stream.body(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )


def _register_action_routes(router, live: _LiveRoutes) -> None:
    from fastapi import Request

    from services.automation import permissions
    from services.automation.control import require_app_control

    Id = _id_param()

    @router.get("/v1/actions", operation_id="list_actions")
    async def list_actions():
        """The user's enabled actions: id, name, kind, description, spoken names."""
        return await live.respond(
            "actions", lambda: {"actions": require_app_control().list_actions()}, status=200
        )

    @router.post(
        "/v1/actions/{action_id}/run", status_code=202, operation_id="run_action",
        openapi_extra=_json_body(
            {"text": _text_field(MAX_ACTION_TEXT, "The words, as if the user had said them.")},
            "text",
        ),
    )
    async def run_action(action_id: Id, request: Request):
        """Run an action on text, as if the user had said it. Body: ``{"text"}``."""
        try:
            body = await read_json_body(request, MAX_RUN_BODY)
        except _Invalid as exc:
            return live.failure("run", exc)

        def work():
            permissions.require(live.saved(), permissions.RUN_ACTIONS)
            text = _string(body, "text", required=True, max_chars=MAX_ACTION_TEXT)
            return {"job_id": require_app_control().run_action(action_id, text, source="api")}

        return await live.respond("run", work)


def _register_recording_routes(router, live: _LiveRoutes) -> None:
    from fastapi import Request

    from services.automation import permissions
    from services.automation.control import require_app_control

    @router.post(
        "/v1/recording/start", status_code=202, operation_id="start_recording",
        openapi_extra=_json_body({"action_id": _text_field(
            MAX_ID_CHARS, "Record words for this action; leave out for a dictation."
        )}),
    )
    async def start_recording(request: Request):
        """Start recording a dictation, or words for ``{"action_id"}``."""
        try:
            body = await read_json_body(request, MAX_SMALL_BODY)
        except _Invalid as exc:
            return live.failure("start", exc)

        def work():
            permissions.require(live.saved(), permissions.CONTROL_RECORDING)
            action_id = _string(body, "action_id", required=False, max_chars=MAX_ID_CHARS)
            job_id = require_app_control().start_recording(action_id=action_id, source="api")
            return {"job_id": job_id}

        return await live.respond("start", work)

    @router.post("/v1/recording/stop", status_code=202, operation_id="stop_recording")
    async def stop_recording():
        """Stop the recording; its words are transcribed and delivered as usual."""

        def work():
            permissions.require(live.saved(), permissions.CONTROL_RECORDING)
            require_app_control().stop_recording()
            return {}

        return await live.respond("stop", work)

    @router.post("/v1/recording/cancel", status_code=202, operation_id="cancel_recording")
    async def cancel_recording():
        """Throw away the recording or the transcription in progress."""

        def work():
            permissions.require(live.saved(), permissions.CONTROL_RECORDING)
            require_app_control().cancel()
            return {}

        return await live.respond("cancel", work)


def _filter_options(body: Mapping[str, Any]) -> dict:
    """``register_lease``'s keyword options from a lease request body."""
    options = {}
    if body.get("timeout_ms") is not None:
        timeout_ms = body["timeout_ms"]
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise _Invalid("timeout_ms must be a whole number of milliseconds.")
        options["timeout_ms"] = timeout_ms
    return options


def _register_filter_routes(router, live: _LiveRoutes) -> None:
    from fastapi import Request

    from services.automation import filters, permissions

    @router.post(
        "/v1/filters", status_code=201, operation_id="create_filter_lease",
        openapi_extra=_json_body(
            {
                "url": _text_field(MAX_URL_CHARS, "Your filter's address on this computer."),
                "stage": {"type": "string", "enum": ["before_cleanup", "after_cleanup"]},
                "timeout_ms": {"type": "integer", "description": "How long to wait for a reply."},
            },
            "url", "stage",
        ),
    )
    async def create_filter(request: Request):
        """Lease a text filter: ``{"url", "stage", "timeout_ms"?}``; renew within 60 s."""
        if not peer_is_local(request):
            # A filter's address is on this computer, which a Tailscale peer
            # must not point OpenWhisper's dictations at.
            return live.failure("filters", _Invalid(
                "Only a program on this computer can lease a text filter.", 403, "forbidden"
            ))
        try:
            body = await read_json_body(request, MAX_SMALL_BODY)
        except _Invalid as exc:
            return live.failure("filters", exc)

        def work():
            permissions.require(live.saved(), permissions.TEXT_FILTERS)
            url = _string(body, "url", required=True, max_chars=MAX_URL_CHARS)
            stage = _string(body, "stage", required=True, max_chars=MAX_ID_CHARS)
            options = _filter_options(body)
            try:
                lease = filters.register_lease(url, stage, **options)
            except ValueError as exc:
                raise _Invalid(str(exc)) from None
            return _lease_dict(lease)

        return await live.respond("filters", work, status=201)


def _register_filter_lease_routes(router, live: _LiveRoutes) -> None:
    from services.automation import filters, permissions
    from services.automation.control import ControlError

    Id = _id_param()

    @router.post("/v1/filters/{lease_id}/renew", operation_id="renew_filter_lease")
    async def renew_filter(lease_id: Id):
        """Keep a leased text filter for another 60 seconds."""

        def work():
            permissions.require(live.saved(), permissions.TEXT_FILTERS)
            try:
                return _lease_dict(filters.renew_lease(lease_id))
            except KeyError:
                raise ControlError("not_found", "That filter lease has expired. Create a new one.") from None

        return await live.respond("filters", work, status=200)

    @router.delete("/v1/filters/{lease_id}", status_code=204, operation_id="delete_filter_lease")
    async def delete_filter(lease_id: Id):
        """Stop a leased text filter now."""

        def work():
            permissions.require(live.saved(), permissions.TEXT_FILTERS)
            if not filters.drop_lease(lease_id):
                raise ControlError("not_found", "That filter lease has expired.")
            return None

        return await live.respond("filters", work, status=204)


def _register_notify_routes(router, live: _LiveRoutes) -> None:
    from fastapi import Request

    from services.automation import permissions
    from services.automation.control import require_app_control

    @router.post(
        "/v1/notify", status_code=202, operation_id="notify_user",
        openapi_extra=_json_body(
            {
                "text": _text_field(MAX_NOTICE_TEXT, "The message."),
                "title": _text_field(MAX_NOTICE_TITLE, "Shown before the message."),
            },
            "text",
        ),
    )
    async def notify(request: Request):
        """Show a short message near the pointer: ``{"text", "title"?}``."""
        try:
            body = await read_json_body(request, MAX_SMALL_BODY)
        except _Invalid as exc:
            return live.failure("notify", exc)

        def work():
            permissions.require(live.saved(), permissions.RUN_ACTIONS)
            text = _string(body, "text", required=True, max_chars=MAX_NOTICE_TEXT)
            title = _string(body, "title", required=False, max_chars=MAX_NOTICE_TITLE)
            if not text.strip():
                raise _Invalid(f"text must be text of 1 to {MAX_NOTICE_TEXT:,} characters.")
            require_app_control().notify(text, title=title)
            return {}

        return await live.respond("notify", work)


def create_live_router(settings, *, enabled: Optional[Callable[[], bool]] = None):
    """The live routes.

    Args:
        settings: The app's SettingsManager; None means default settings
            (live events on, every other permission off).
        enabled: False once MCP is switched off or the listener is stopping;
            open event streams end within half a second.
    """
    from fastapi import APIRouter

    router = APIRouter()
    live = _LiveRoutes(settings, enabled)
    _register_state_routes(router, live)
    _register_action_routes(router, live)
    _register_recording_routes(router, live)
    _register_filter_routes(router, live)
    _register_filter_lease_routes(router, live)
    _register_notify_routes(router, live)
    return router
