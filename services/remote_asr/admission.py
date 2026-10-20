"""Bounded, per-device admission for a host's expensive requests."""
from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Callable


@dataclass(eq=False)
class _Ticket:
    device_id: str
    priority: int


class WorkAdmission:
    """Serve waiting devices fairly, with durable text ahead of background work.

    One device may own one running and one waiting request. Opening more
    connections therefore cannot buy more places in the queue. Disposable
    previews only use idle capacity. Stateful pushes instead receive the usual
    admission or an explicit busy refusal; none are silently shed.
    """

    def __init__(self, active: int, queued: int, wait_s: float) -> None:
        self._active_limit = active
        self._queue_limit = queued
        self._wait_s = wait_s
        self._condition = threading.Condition()
        self._active = 0
        self._devices: Counter = Counter()
        self._queue = deque()

    def acquire(self, still_connected: Callable[[], bool], *, device_id: str = "",
                priority: int = 0, preview: bool = False) -> bool:
        with self._condition:
            if not still_connected():
                return False
            if not self._queue and self._available(device_id):
                self._take(device_id)
                return True
            if preview or not self._can_queue(device_id):
                return False
            ticket = _Ticket(device_id, priority)
            self._queue.append(ticket)
            try:
                return self._wait(ticket, still_connected)
            finally:
                if ticket in self._queue:
                    self._queue.remove(ticket)
                    self._condition.notify_all()

    def _can_queue(self, device_id: str) -> bool:
        return (len(self._queue) < self._queue_limit
                and not any(ticket.device_id == device_id for ticket in self._queue))

    def _available(self, device_id: str) -> bool:
        return self._active < self._active_limit and not self._devices[device_id]

    def _take(self, device_id: str) -> None:
        self._active += 1
        self._devices[device_id] += 1

    def _next(self):
        eligible = (ticket for ticket in self._queue if self._available(ticket.device_id))
        return min(eligible, key=lambda ticket: ticket.priority, default=None)

    def _wait(self, ticket: _Ticket, still_connected: Callable[[], bool]) -> bool:
        deadline = time.monotonic() + self._wait_s
        while still_connected() and time.monotonic() < deadline:
            if self._next() is ticket:
                self._queue.remove(ticket)
                self._take(ticket.device_id)
                self._condition.notify_all()
                return True
            self._condition.wait(timeout=0.1)
        return False

    def release(self, device_id: str = "") -> None:
        with self._condition:
            self._active -= 1
            self._devices[device_id] -= 1
            if not self._devices[device_id]:
                del self._devices[device_id]
            self._condition.notify_all()

    def snapshot(self) -> dict:
        with self._condition:
            return {"active": self._active, "queued": len(self._queue),
                    "active_limit": self._active_limit, "queue_limit": self._queue_limit,
                    "queued_by_priority": dict(Counter(ticket.priority for ticket in self._queue))}
