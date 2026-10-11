"""The in-process event bus behind the live API, webhooks, programs and MCP Events.

Publishers on any thread (the Qt thread, transcription workers, the API's
event loop) only append to a buffer; one dispatch thread hands each event to
the subscribers, whose callbacks must only enqueue. So a slow webhook or a
stalled stream never delays a paste, and no subscriber runs on the Qt thread.

Events carry dictated text. They are kept in memory only (a short replay
buffer for reconnecting streams) and are never logged.
"""

from __future__ import annotations

import itertools
import json
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

#: The ``v`` every event carries; bumped only for incompatible payload changes.
SCHEMA_VERSION = 1
DEFAULT_BUFFER_SIZE = 500
#: A callback slower than this is reported once, by type name only.
SLOW_CALLBACK_S = 1.0


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class Event:
    id: str
    seq: int
    type: str
    time: str
    data: Mapping[str, Any]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "seq": self.seq,
            "type": self.type,
            "time": self.time,
            "v": SCHEMA_VERSION,
            "data": dict(self.data),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


class Subscription:
    """One subscriber; ``close()`` stops further deliveries (idempotent)."""

    def __init__(self, bus: "EventBus", callback: Callable[[Event], None],
                 types: Optional[frozenset], label: str) -> None:
        self._bus = bus
        self.callback = callback
        self.types = types
        self.label = label
        self._closed = False
        self._warned_slow = False

    @property
    def closed(self) -> bool:
        return self._closed

    def wants(self, event: Event) -> bool:
        return self.types is None or event.type in self.types

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._remove(self)


def _types(types: Optional[Iterable[str]]) -> Optional[frozenset]:
    if types is None:
        return None
    return frozenset(t for t in types if isinstance(t, str) and t)


class EventBus:
    """Thread-safe publish/subscribe with a bounded replay buffer."""

    def __init__(self, *, buffer_size: int = DEFAULT_BUFFER_SIZE) -> None:
        self._lock = threading.Condition()
        self._buffer: deque[Event] = deque(maxlen=max(1, int(buffer_size)))
        self._pending: deque[Event] = deque()
        self._subscriptions: list[Subscription] = []
        self._seq = itertools.count(1)
        self._last_seq = 0
        # The newest event no longer in the buffer (evicted or cleared).
        self._evicted_seq = 0
        self._thread: Optional[threading.Thread] = None
        self._closed = False

    # -- publishing ---------------------------------------------------------

    def publish(self, event_type: str, data: Mapping[str, Any], *,
                replay: bool = True) -> Event:
        """Record and queue an event; any thread, never blocks on subscribers.

        Args:
            replay: Keep it for ``since``/``recent``. Frequent, short-lived
                events (live preview text) pass False so they never push
                meaningful events out of the buffer.

        Raises:
            TypeError: ``data`` is not JSON-serializable.
        """
        payload = dict(data or {})
        # Fail here, at the publisher, rather than in every subscriber.
        json.dumps(payload, ensure_ascii=False)
        with self._lock:
            seq = next(self._seq)
            event = Event(
                id="evt_" + secrets.token_hex(12),
                seq=seq,
                type=event_type,
                time=_now_iso(),
                data=payload,
            )
            self._last_seq = seq
            if replay:
                if len(self._buffer) == self._buffer.maxlen:
                    self._evicted_seq = self._buffer[0].seq
                self._buffer.append(event)
            if self._subscriptions and not self._closed:
                self._pending.append(event)
                self._ensure_thread()
                self._lock.notify()
        return event

    # -- subscribing --------------------------------------------------------

    def subscribe(self, callback: Callable[[Event], None], *,
                  types: Optional[Iterable[str]] = None,
                  label: str = "") -> Subscription:
        """Call ``callback(event)`` on the dispatch thread for each new event.

        Args:
            callback: Must return quickly (enqueue and return).
            types: Only these event types; None for all.
            label: Lets ``subscribers(label)`` count a kind of listener.
        """
        subscription = Subscription(self, callback, _types(types), label)
        with self._lock:
            if self._closed:
                subscription._closed = True
                return subscription
            self._subscriptions.append(subscription)
        return subscription

    def _remove(self, subscription: Subscription) -> None:
        with self._lock:
            try:
                self._subscriptions.remove(subscription)
            except ValueError:
                pass

    def subscribers(self, label: str = "") -> int:
        with self._lock:
            return sum(
                1 for s in self._subscriptions
                if not s.closed and (not label or s.label == label)
            )

    # -- replay -------------------------------------------------------------

    def since(self, seq: int, *, types: Optional[Iterable[str]] = None
              ) -> tuple[list[Event], bool]:
        """Buffered events after ``seq``, and whether some were already dropped."""
        wanted = _types(types)
        with self._lock:
            buffered = list(self._buffer)
            last = self._last_seq
            evicted = self._evicted_seq
        if seq > last:
            # A position from an earlier app session (numbering restarts
            # with the app): everything buffered is new to this client.
            seq, gap = 0, True
        else:
            gap = seq < evicted
        events = [
            e for e in buffered
            if e.seq > seq and (wanted is None or e.type in wanted)
        ]
        return events, gap

    def recent(self, limit: int = 50, *,
               types: Optional[Iterable[str]] = None) -> list[Event]:
        """The newest ``limit`` buffered events, oldest first."""
        wanted = _types(types)
        with self._lock:
            buffered = list(self._buffer)
        events = [e for e in buffered if wanted is None or e.type in wanted]
        return events[-max(0, int(limit)):] if limit > 0 else []

    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq

    def clear(self) -> None:
        """Forget buffered events (history was cleared); sequence numbers continue."""
        with self._lock:
            self._buffer.clear()
            self._evicted_seq = self._last_seq

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending.clear()
            for subscription in self._subscriptions:
                subscription._closed = True
            self._subscriptions.clear()
            self._lock.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    # -- dispatch -----------------------------------------------------------

    def _ensure_thread(self) -> None:
        # Caller holds the lock.
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, name="automation-events", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._pending and not self._closed:
                    self._lock.wait()
                if self._closed:
                    return
                event = self._pending.popleft()
                subscriptions = [s for s in self._subscriptions if s.wants(event)]
            for subscription in subscriptions:
                if subscription.closed:
                    continue
                started = time.monotonic()
                try:
                    subscription.callback(event)
                except Exception as exc:
                    logger.warning(
                        "Event subscriber %s failed: %s",
                        subscription.label or "(unnamed)", type(exc).__name__,
                    )
                elapsed = time.monotonic() - started
                if elapsed > SLOW_CALLBACK_S and not subscription._warned_slow:
                    subscription._warned_slow = True
                    logger.warning(
                        "Event subscriber %s took %.1f s; callbacks must only enqueue",
                        subscription.label or "(unnamed)", elapsed,
                    )


_bus: Optional[EventBus] = None
_bus_lock = threading.Lock()


def bus() -> EventBus:
    """The process-wide bus, created on first use."""
    global _bus
    with _bus_lock:
        if _bus is None:
            _bus = EventBus()
        return _bus


def set_bus(replacement: Optional[EventBus]) -> Optional[EventBus]:
    """Swap the process-wide bus (tests); returns the previous one."""
    global _bus
    with _bus_lock:
        previous, _bus = _bus, replacement
        return previous


def publish(event_type: str, data: Mapping[str, Any], *,
            replay: bool = True) -> Optional[Event]:
    """Publish on the process-wide bus; never raises."""
    try:
        return bus().publish(event_type, data, replay=replay)
    except Exception as exc:
        logger.debug("Could not publish %s: %s", event_type, type(exc).__name__)
        return None
