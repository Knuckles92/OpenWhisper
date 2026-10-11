"""Signed webhook delivery for saved webhooks (Settings > Hooks).

Each enabled webhook receives the events it chose as the event's wire JSON
(``Event.to_dict()``), POSTed with Standard Webhooks headers signed by a
per-webhook secret kept in the credential store.

One bus subscription covers every enabled webhook; its callback only
enqueues. One worker thread delivers from a bounded queue in event order per
webhook. A failed attempt that is worth retrying (a network error, 408, 429,
5xx) waits at the head of its webhook's queue until its backoff passes, so
later events for that webhook stay behind it while other webhooks keep
flowing; the worker never sleeps on a retry. Redirects are never followed.

Requests to loopback and private addresses ignore proxy settings, so local
traffic never reaches a system proxy. Event data, URLs and secrets are never
logged. Contract: docs/design/automation/design.md.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import math
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import httpx

    from services.automation.events import Event, EventBus, Subscription
    from services.automation.models import Webhook

logger = logging.getLogger(__name__)

#: Per attempt: connecting, sending and waiting for the answer each.
TIMEOUT_S = 5.0
#: Waits before the second, third and fourth attempt.
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 5.0, 25.0)
#: The longest a ``Retry-After`` answer may hold a webhook back.
RETRY_AFTER_MAX_S = 60.0
#: Deliveries waiting across all webhooks; the oldest is dropped beyond this.
MAX_QUEUED = 500
#: Recent deliveries kept per webhook (memory only).
MAX_RECORDS = 50
#: Most of a reply ``post_signed_reply`` reads.
MAX_REPLY_BYTES = 1024 * 1024
TEST_EVENT_TYPE = "webhook.test"
SUBSCRIPTION_LABEL = "webhooks"
CONTENT_TYPE = "application/json"


@dataclass(frozen=True)
class DeliveryRecord:
    event_id: str
    event_type: str
    at: str
    ok: bool
    status: Optional[int]
    elapsed_ms: int
    error: str = ""
    attempt: int = 1


class SecretUnavailable(RuntimeError):
    """The credential store could not keep a webhook's signing secret.

    The message is user-facing and never contains a secret.
    """


def _iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def user_agent() -> str:
    try:
        from _version import __version__
    except Exception:
        return "OpenWhisper"
    return f"OpenWhisper/{__version__}"


def _synthetic_event(event_type: str, data: Mapping[str, Any]) -> Event:
    """An event for a test delivery; never published on the bus (``seq`` 0)."""
    from services.automation.events import Event

    return Event(
        id="evt_" + secrets.token_hex(12), seq=0, type=event_type,
        time=_iso_now(), data=dict(data),
    )


def _load_settings() -> Mapping:
    from services import settings

    return settings.settings_manager.load_all_settings() or {}


# -- secrets ------------------------------------------------------------------

_secret_lock = threading.Lock()


def _secret_name(hook_id: str) -> str:
    from services.automation.models import webhook_secret_name

    if not isinstance(hook_id, str) or not hook_id.strip():
        raise ValueError("This webhook has no id.")
    return webhook_secret_name(hook_id.strip())


def _store_copy(exc: Exception) -> str:
    from services.credentials import CredentialStoreError

    if isinstance(exc, CredentialStoreError):
        return f"Couldn't keep the webhook's signing secret. {exc}"
    return f"Couldn't keep the webhook's signing secret ({type(exc).__name__})."


def ensure_secret(hook_id: str) -> str:
    """The webhook's signing secret, created on first use.

    Raises:
        SecretUnavailable: the credential store can't read or save it.
        ValueError: ``hook_id`` is empty.
    """
    from services.automation import signing
    from services.credentials import store

    name = _secret_name(hook_id)
    with _secret_lock:
        try:
            existing = store().get(name)
            if existing:
                return existing
            secret = signing.new_secret()
            store().set(name, secret)
        except Exception as exc:
            raise SecretUnavailable(_store_copy(exc)) from exc
    return secret


def rotate_secret(hook_id: str) -> str:
    """Replace the webhook's signing secret; returns the new one.

    Raises:
        SecretUnavailable: the credential store can't save it.
    """
    from services.automation import signing
    from services.credentials import store

    name = _secret_name(hook_id)
    secret = signing.new_secret()
    with _secret_lock:
        try:
            store().set(name, secret)
        except Exception as exc:
            raise SecretUnavailable(_store_copy(exc)) from exc
    return secret


def delete_secret(hook_id: str) -> None:
    """Forget a deleted webhook's secret; never raises."""
    from services.credentials import store

    try:
        name = _secret_name(hook_id)
        with _secret_lock:
            store().delete(name)
    except Exception as exc:
        logger.warning("Could not remove a webhook's signing secret: %s", type(exc).__name__)


# -- one HTTP exchange --------------------------------------------------------

@dataclass(frozen=True)
class _Result:
    ok: bool
    status: Optional[int]
    elapsed_ms: int
    error: str = ""
    retryable: bool = False
    retry_after: Optional[float] = None
    body: bytes = b""


class _TooLarge(Exception):
    pass


class _Overdue(Exception):
    pass


#: Set while the hooks engine makes a request, so httpx's per-request log
#: lines (which carry the full URL) are dropped for it.
_quiet = contextvars.ContextVar("openwhisper_hooks_request", default=False)
#: httpx logs every request's URL at INFO; httpcore logs hosts at DEBUG.
_REQUEST_LOGGERS = ("httpx", "httpcore.connection", "httpcore.http11",
                    "httpcore.http2", "httpcore.proxy", "httpcore.socks")


class _QuietRequests(logging.Filter):
    """Drops a request log record made by the hooks engine; other requests keep theirs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _quiet.get()


_quiet_installed = False
_quiet_lock = threading.Lock()


def _install_quiet_requests() -> None:
    global _quiet_installed
    with _quiet_lock:
        if _quiet_installed:
            return
        for name in _REQUEST_LOGGERS:
            logging.getLogger(name).addFilter(_QuietRequests())
        _quiet_installed = True


def _new_client(private: bool) -> httpx.Client:
    """A client that never follows redirects; private addresses skip proxies."""
    import httpx

    _install_quiet_requests()
    return httpx.Client(
        follow_redirects=False,
        trust_env=not private,
        timeout=TIMEOUT_S,
        headers={"User-Agent": user_agent()},
    )


def _is_private(url: str) -> bool:
    from services.automation.models import url_is_private

    return url_is_private(url)


def _retry_after(value: Optional[str]) -> Optional[float]:
    """Seconds a ``Retry-After`` header asks for (delta or HTTP date), or None."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def no_answer_copy(timeout_s: float) -> str:
    """How a request that timed out is described ("No answer within 1.5 s")."""
    return f"No answer within {round(timeout_s, 3):g} s"


def _read_capped(response, limit: int, deadline: Optional[float]) -> bytes:
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise _TooLarge()
        chunks.append(chunk)
        if deadline is not None and time.monotonic() > deadline:
            raise _Overdue()
    return b"".join(chunks)


def _transport_copy(exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.ConnectError):
        return "Couldn't connect"
    if isinstance(exc, httpx.ProxyError):
        return "The proxy refused the request"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "The server closed the connection"
    if isinstance(exc, httpx.NetworkError):
        return "The connection broke"
    return f"The request failed ({type(exc).__name__})"


def _exchange(client: httpx.Client, url: str, body: bytes, headers: Mapping[str, str], *,
              timeout_s: float, read_limit: int = 0,
              deadline: Optional[float] = None) -> _Result:
    """POST ``body`` once and classify the answer; never raises.

    Args:
        read_limit: Read up to this many bytes of a 2xx reply; 0 reads none.
        deadline: ``time.monotonic()`` after which reading the reply stops.

    Errors are fixed copy (never the URL or the reply). ``retryable`` marks
    network errors, timeouts, 408, 429 and 5xx; a 3xx is a failure because
    redirects are never followed.
    """
    import httpx

    started = time.monotonic()

    def result(**fields) -> _Result:
        return _Result(elapsed_ms=int((time.monotonic() - started) * 1000), **fields)

    status: Optional[int] = None
    timed_out = no_answer_copy(timeout_s)
    quiet = _quiet.set(True)
    try:
        with client.stream("POST", url, content=body, headers=dict(headers),
                           timeout=timeout_s) as response:
            status = response.status_code
            if 200 <= status < 300:
                data = _read_capped(response, read_limit, deadline) if read_limit else b""
                return result(ok=True, status=status, body=data)
            retry_after = _retry_after(response.headers.get("retry-after"))
    except _TooLarge:
        return result(ok=False, status=status, error="The reply was too large")
    except (_Overdue, httpx.TimeoutException):
        return result(ok=False, status=status, error=timed_out, retryable=True)
    except (httpx.UnsupportedProtocol, httpx.LocalProtocolError, httpx.InvalidURL) as exc:
        return result(ok=False, status=status,
                      error=f"The address can't be used ({type(exc).__name__})")
    except httpx.TransportError as exc:
        return result(ok=False, status=status, error=_transport_copy(exc), retryable=True)
    except Exception as exc:
        return result(ok=False, status=status,
                      error=f"The request failed ({type(exc).__name__})")
    finally:
        _quiet.reset(quiet)
    if 300 <= status < 400:
        return result(ok=False, status=status,
                      error=f"HTTP {status}: redirects aren't followed")
    retryable = status in (408, 429) or status >= 500
    return result(ok=False, status=status, error=f"HTTP {status}", retryable=retryable,
                  retry_after=retry_after if retryable else None)


def _signed_headers(secret: str, msg_id: str, body: bytes) -> dict[str, str]:
    """Raises ValueError (user-facing) when ``secret`` is not a whsec_ secret."""
    from services.automation import signing

    return {"Content-Type": CONTENT_TYPE, **signing.headers(secret, msg_id, body)}


# -- one-off signed POSTs (actions, MCP Events) ---------------------------------

def _post_signed(url: str, payload: Mapping, secret: str, *, timeout_s: float,
                 read_limit: int, msg_id: str,
                 headers: Optional[Mapping[str, str]]) -> tuple[DeliveryRecord, Optional[bytes]]:
    from services.automation.models import validate_url

    at = _iso_now()
    payload = payload if isinstance(payload, Mapping) else {}
    raw_id, raw_type = payload.get("id"), payload.get("type")
    if not (isinstance(msg_id, str) and msg_id):
        msg_id = raw_id if isinstance(raw_id, str) and raw_id else "msg_" + secrets.token_hex(12)
    event_type = raw_type if isinstance(raw_type, str) else ""

    def failed(error: str) -> tuple[DeliveryRecord, None]:
        return DeliveryRecord(msg_id, event_type, at, False, None, 0, error, 1), None

    try:
        validate_url(url)
    except ValueError as exc:
        return failed(str(exc))
    try:
        body = json.dumps(dict(payload), ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return failed("The message isn't valid JSON.")
    try:
        signed = _signed_headers(secret, msg_id, body)
    except ValueError as exc:
        return failed(str(exc))
    # Extra headers never replace the signature or the content type.
    extra = {str(k): str(v) for k, v in (headers or {}).items()
             if str(k).lower() not in {h.lower() for h in signed}}
    timeout = float(timeout_s) if timeout_s and timeout_s > 0 else TIMEOUT_S
    with _new_client(_is_private(url)) as client:
        outcome = _exchange(client, url, body, {**extra, **signed}, timeout_s=timeout,
                            read_limit=read_limit,
                            deadline=time.monotonic() + timeout if read_limit else None)
    record = DeliveryRecord(msg_id, event_type, at, outcome.ok, outcome.status,
                            outcome.elapsed_ms, outcome.error, 1)
    return record, outcome.body if outcome.ok else None


def post_signed(url: str, payload: dict, secret: str, *, timeout_s: float = TIMEOUT_S,
                msg_id: str = "", headers: Optional[Mapping[str, str]] = None
                ) -> DeliveryRecord:
    """POST ``payload`` as JSON signed with ``secret``; one attempt, never raises.

    Args:
        msg_id: The ``webhook-id``; "" uses ``payload["id"]`` when it is a
            non-empty string, else a fresh ``msg_`` id.
        headers: Extra request headers (e.g. ``X-MCP-Subscription-Id``);
            they never replace Content-Type or the signature headers.

    The record's ``event_type`` is ``payload["type"]`` when present.
    Redirects are not followed; loopback and private addresses skip proxies.
    """
    try:
        return _post_signed(url, payload, secret, timeout_s=timeout_s, read_limit=0,
                            msg_id=msg_id, headers=headers)[0]
    except Exception as exc:
        return DeliveryRecord(msg_id or "", "", _iso_now(), False, None, 0,
                              f"The request failed ({type(exc).__name__}).", 1)


def post_signed_reply(url: str, payload: dict, secret: str, *, timeout_s: float = TIMEOUT_S,
                      max_bytes: int = MAX_REPLY_BYTES, msg_id: str = "",
                      headers: Optional[Mapping[str, str]] = None
                      ) -> tuple[DeliveryRecord, Optional[bytes]]:
    """``post_signed`` that also reads the reply body; never raises.

    Returns:
        ``(record, body)``: ``body`` is the 2xx reply's bytes (``b""`` when
        it had none, e.g. 204), or None when the POST failed. A reply longer
        than ``max_bytes`` is a failure ("The reply was too large").
        ``timeout_s`` bounds connecting, sending and reading the reply. A
        webhook action decodes the body itself, e.g. ``json.loads`` and
        reads ``"text"``.
    """
    try:
        limit = int(max_bytes) if max_bytes and max_bytes > 0 else MAX_REPLY_BYTES
        return _post_signed(url, payload, secret, timeout_s=timeout_s, read_limit=limit,
                            msg_id=msg_id, headers=headers)
    except Exception as exc:
        return DeliveryRecord(msg_id or "", "", _iso_now(), False, None, 0,
                              f"The request failed ({type(exc).__name__}).", 1), None


# -- delivery to saved webhooks -------------------------------------------------

_records_lock = threading.Lock()
_records: dict[str, deque] = {}


def _remember(hook_id: str, record: DeliveryRecord) -> None:
    with _records_lock:
        _records.setdefault(hook_id, deque(maxlen=MAX_RECORDS)).appendleft(record)


def deliveries(hook_id: str) -> list[DeliveryRecord]:
    """Recent delivery attempts to one webhook, newest first (memory only)."""
    with _records_lock:
        return list(_records.get(hook_id, ()))


def _forget_records(keep: set) -> None:
    with _records_lock:
        for hook_id in [h for h in _records if h not in keep]:
            del _records[hook_id]


def _deliver(hook: Webhook, event: Event, attempt: int,
             client_for: Callable[[bool], httpx.Client]
             ) -> tuple[DeliveryRecord, Optional[float]]:
    """One attempt; returns the record and the wait before a retry (None: done)."""
    at = _iso_now()
    try:
        secret = ensure_secret(hook.id)
        body = event.to_json().encode("utf-8")
        headers = _signed_headers(secret, event.id, body)
    except SecretUnavailable as exc:
        return DeliveryRecord(event.id, event.type, at, False, None, 0, str(exc), attempt), None
    except ValueError:
        return DeliveryRecord(
            event.id, event.type, at, False, None, 0,
            "The signing secret is damaged. Make a new one in Settings.", attempt,
        ), None
    outcome = _exchange(client_for(_is_private(hook.url)), hook.url, body, headers,
                        timeout_s=TIMEOUT_S)
    record = DeliveryRecord(event.id, event.type, at, outcome.ok, outcome.status,
                            outcome.elapsed_ms, outcome.error, attempt)
    if outcome.ok or not outcome.retryable or attempt > len(RETRY_DELAYS_S):
        return record, None
    delay = RETRY_DELAYS_S[attempt - 1]
    if outcome.retry_after is not None:
        delay = max(delay, min(outcome.retry_after, RETRY_AFTER_MAX_S))
    return record, delay


@dataclass
class _Item:
    event: Event
    serial: int
    attempt: int = 1
    not_before: float = 0.0


class _Engine:
    """The subscription, the queue and the delivery worker."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._configure_lock = threading.Lock()
        self._hooks: dict[str, Webhook] = {}
        self._queues: dict[str, deque] = {}
        self._queued = 0
        self._serial = 0
        self._dropped = 0
        self._bus: Optional[EventBus] = None
        self._subscription: Optional[Subscription] = None
        self._types: frozenset = frozenset()
        self._last_seq = 0
        self._stopping = False
        self._thread: Optional[threading.Thread] = None

    # -- configuration (any thread) ------------------------------------------

    def configure(self, hooks: list, bus: EventBus) -> None:
        enabled = {h.id: h for h in hooks if h.enabled and h.events}
        types = frozenset(t for h in enabled.values() for t in h.events)
        with self._configure_lock:
            with self._cond:
                if self._stopping:
                    return
                self._hooks = enabled
                for hook_id in list(self._queues):
                    hook = enabled.get(hook_id)
                    queue = self._queues[hook_id]
                    kept = deque(i for i in queue if hook is not None and i.event.type in hook.events)
                    self._queued -= len(queue) - len(kept)
                    if kept:
                        self._queues[hook_id] = kept
                    else:
                        del self._queues[hook_id]
                if bus is self._bus and types == self._types:
                    return
                old = self._subscription
                if bus is not self._bus:
                    self._bus, self._last_seq = bus, 0
                self._types = types
            # Subscribe before closing the old one so no event slips between
            # them; one delivered to both is dropped by its sequence number.
            new = (bus.subscribe(functools.partial(self._on_event, bus), types=types,
                                 label=SUBSCRIPTION_LABEL) if types else None)
            with self._cond:
                self._subscription = new
            if old is not None:
                old.close()

    def stop(self) -> None:
        with self._configure_lock:
            with self._cond:
                self._stopping = True
                self._queues.clear()
                self._queued = 0
                subscription, self._subscription = self._subscription, None
                thread = self._thread
                self._cond.notify_all()
            if subscription is not None:
                subscription.close()
        if thread is not None and thread is not threading.current_thread():
            # An attempt in flight finishes on its own (daemon thread).
            thread.join(timeout=1.0)

    # -- the bus callback (dispatch thread; only enqueues) ---------------------

    def _on_event(self, bus: EventBus, event: Event) -> None:
        with self._cond:
            if self._stopping or bus is not self._bus or event.seq <= self._last_seq:
                return
            self._last_seq = event.seq
            for hook in self._hooks.values():
                if event.type in hook.events:
                    self._serial += 1
                    self._enqueue(hook.id, _Item(event, self._serial))

    def _enqueue(self, hook_id: str, item: _Item, *, front: bool = False) -> None:
        # Caller holds the condition.
        queue = self._queues.setdefault(hook_id, deque())
        if front:
            queue.appendleft(item)
        else:
            queue.append(item)
        self._queued += 1
        while self._queued > MAX_QUEUED:
            self._drop_oldest()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, name="automation-webhooks", daemon=True
            )
            self._thread.start()
        self._cond.notify()

    def _drop_oldest(self) -> None:
        hook_id = min(
            (h for h, q in self._queues.items() if q),
            key=lambda h: self._queues[h][0].serial,
        )
        item = self._queues[hook_id].popleft()
        if not self._queues[hook_id]:
            del self._queues[hook_id]
        self._queued -= 1
        self._dropped += 1
        if self._dropped == 1 or self._dropped % 100 == 0:
            logger.warning(
                "Webhook deliveries are backing up; dropped the oldest (%d so far)",
                self._dropped,
            )
        _remember(hook_id, DeliveryRecord(
            item.event.id, item.event.type, _iso_now(), False, None, 0,
            "Dropped: too many deliveries were waiting.", item.attempt,
        ))

    # -- the worker -----------------------------------------------------------

    def _next(self) -> Optional[tuple[Webhook, _Item]]:
        """The oldest delivery that is due, waiting as needed; None when stopping."""
        with self._cond:
            while not self._stopping:
                now = time.monotonic()
                due, wake = None, None
                for hook_id, queue in self._queues.items():
                    head = queue[0]
                    if head.not_before <= now:
                        if due is None or head.serial < due[1].serial:
                            due = (hook_id, head)
                    else:
                        wait = head.not_before - now
                        wake = wait if wake is None else min(wake, wait)
                if due is not None:
                    hook_id, item = due
                    queue = self._queues[hook_id]
                    queue.popleft()
                    if not queue:
                        del self._queues[hook_id]
                    self._queued -= 1
                    return self._hooks[hook_id], item
                self._cond.wait(timeout=wake)
            return None

    def _run(self) -> None:
        clients: dict[bool, httpx.Client] = {}

        def client_for(private: bool) -> httpx.Client:
            if private not in clients:
                clients[private] = _new_client(private)
            return clients[private]

        try:
            while True:
                picked = self._next()
                if picked is None:
                    return
                hook, item = picked
                try:
                    record, delay = _deliver(hook, item.event, item.attempt, client_for)
                except Exception as exc:
                    logger.warning("Webhook delivery failed: %s", type(exc).__name__)
                    record, delay = DeliveryRecord(
                        item.event.id, item.event.type, _iso_now(), False, None, 0,
                        f"The delivery failed ({type(exc).__name__}).", item.attempt,
                    ), None
                _remember(hook.id, record)
                if delay is None:
                    continue
                with self._cond:
                    current = self._hooks.get(hook.id)
                    if self._stopping or current is None or item.event.type not in current.events:
                        continue
                    item.attempt += 1
                    item.not_before = time.monotonic() + delay
                    self._enqueue(hook.id, item, front=True)
        finally:
            for client in clients.values():
                try:
                    client.close()
                except Exception:
                    pass


_engine_lock = threading.Lock()
_engine: Optional[_Engine] = None


def _configure(engine: _Engine) -> None:
    from services.automation import events
    from services.automation.models import load_webhooks

    hooks = load_webhooks(_load_settings())
    _forget_records({h.id for h in hooks})
    engine.configure(hooks, events.bus())


def start() -> None:
    """Begin delivering events to enabled webhooks; idempotent."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = _Engine()
        engine = _engine
    _configure(engine)


def stop() -> None:
    """Stop delivering; queued deliveries are dropped. Safe at quit."""
    global _engine
    with _engine_lock:
        engine, _engine = _engine, None
    if engine is not None:
        engine.stop()


def reload() -> None:
    """Saved webhooks changed: deliver to the enabled ones from now on."""
    with _engine_lock:
        engine = _engine
    if engine is not None:
        _configure(engine)


def send_test(hook_id: str) -> Future:
    """Send a ``webhook.test`` event to one saved webhook, once, without retries.

    Works whether or not the webhook is enabled. Runs on its own thread.

    Returns:
        Future[DeliveryRecord]; it never holds an exception.
    """
    future: Future = Future()

    def work() -> None:
        try:
            record = _send_test(hook_id)
        except Exception as exc:
            record = DeliveryRecord("", TEST_EVENT_TYPE, _iso_now(), False, None, 0,
                                    f"The test couldn't run ({type(exc).__name__}).", 1)
        future.set_result(record)

    threading.Thread(target=work, name="webhook-test", daemon=True).start()
    return future


def _send_test(hook_id: str) -> DeliveryRecord:
    from services.automation.models import find_webhook

    hook = find_webhook(_load_settings(), hook_id)
    if hook is None:
        return DeliveryRecord("", TEST_EVENT_TYPE, _iso_now(), False, None, 0,
                              "Save the webhook first.", 1)
    event = _synthetic_event(TEST_EVENT_TYPE, {
        "message": "This is a test delivery from OpenWhisper.",
        "webhook_id": hook.id,
    })
    clients: dict[bool, httpx.Client] = {}

    def client_for(private: bool) -> httpx.Client:
        if private not in clients:
            clients[private] = _new_client(private)
        return clients[private]

    try:
        record, _delay = _deliver(hook, event, 1, client_for)
    finally:
        for client in clients.values():
            client.close()
    _remember(hook.id, record)
    return record
