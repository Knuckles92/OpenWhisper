"""What paired computers have asked of this host since sharing started.

Kept in memory only: the counts start over when sharing starts again or the
app restarts, and nothing here is written to disk. ``SpeechHost`` records
into it from its connection threads; the host dashboard reads snapshots.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable, Deque, Dict, Optional

#: Events kept for the dashboard's recent activity list.
MAX_EVENTS = 60
#: A failure's reason is cut to this many characters in the event list.
MAX_REASON_CHARS = 160


@dataclass
class DeviceActivity:
    """One paired computer's requests since sharing started."""

    name: str
    transcriptions: int = 0
    audio_s: float = 0.0
    host_s: float = 0.0
    previews: int = 0
    errors: int = 0
    last_at: float = 0.0


def empty_snapshot(since: float = 0.0) -> dict:
    return {"since": since, "transcriptions": 0, "audio_s": 0.0, "host_s": 0.0,
            "previews": 0, "errors": 0, "last_at": 0.0, "serial": 0,
            "devices": {}, "events": []}


class HostActivity:
    """Totals, per-computer counts, and a short list of recent events.

    Events are dicts with ``serial`` (increasing), ``at`` (wall clock),
    ``kind`` (transcribed, failed, connected, disconnected, paired,
    switched) and ``name``, plus what the kind needs: ``audio_s`` and
    ``host_s`` for a transcription, ``reason`` for a failure, ``via`` for a
    pairing, ``label`` for a model switch.
    """

    def __init__(self, clock: Callable[[], float] = time.time, max_events: int = MAX_EVENTS):
        self._clock = clock
        self._lock = threading.Lock()
        self._max_events = max_events
        self._events: Deque[dict] = deque(maxlen=max_events)
        self._devices: Dict[str, DeviceActivity] = {}
        #: Open connections per device; a computer's dictation and a meeting
        #: each hold one, so "connected" is logged for the first only.
        self._connections: Dict[str, int] = {}
        self._serial = 0
        self.started_at = clock()

    def reset(self) -> None:
        """Start counting again (sharing just started)."""
        with self._lock:
            self._events.clear()
            self._devices.clear()
            self._connections.clear()
            self.started_at = self._clock()

    # ---- recording ----

    def _device(self, device_id: str, name: str) -> DeviceActivity:
        device = self._devices.get(device_id)
        if device is None:
            device = self._devices[device_id] = DeviceActivity(name)
        elif name:
            device.name = name
        return device

    def _event(self, kind: str, name: str, **fields) -> None:
        self._serial += 1
        self._events.append({"serial": self._serial, "at": self._clock(), "kind": kind,
                             "name": name, **fields})

    def transcribed(self, device_id: str, name: str, audio_s: float, host_s: float) -> None:
        with self._lock:
            device = self._device(device_id, name)
            device.transcriptions += 1
            device.audio_s += max(0.0, audio_s)
            device.host_s += max(0.0, host_s)
            device.last_at = self._clock()
            self._event("transcribed", name, audio_s=round(audio_s, 2), host_s=round(host_s, 3))

    def previewed(self, device_id: str, name: str) -> None:
        """A live preview window: counted, but too frequent to list."""
        with self._lock:
            device = self._device(device_id, name)
            device.previews += 1
            device.last_at = self._clock()

    def failed(self, device_id: str, name: str, reason: str) -> None:
        with self._lock:
            device = self._device(device_id, name)
            device.errors += 1
            device.last_at = self._clock()
            reason = " ".join(str(reason or "").split())
            if len(reason) > MAX_REASON_CHARS:
                reason = reason[:MAX_REASON_CHARS - 1].rstrip() + "…"
            self._event("failed", name, reason=reason)

    def connected(self, device_id: str, name: str) -> None:
        with self._lock:
            count = self._connections.get(device_id, 0) + 1
            self._connections[device_id] = count
            self._device(device_id, name)
            if count == 1:
                self._event("connected", name)

    def disconnected(self, device_id: str, name: str) -> None:
        with self._lock:
            count = self._connections.get(device_id, 0)
            if count <= 0:
                # A connection from before the last reset.
                return
            if count == 1:
                del self._connections[device_id]
                self._event("disconnected", name)
            else:
                self._connections[device_id] = count - 1

    def paired(self, device_id: str, name: str, via: str) -> None:
        with self._lock:
            self._device(device_id, name)
            self._event("paired", name, via=via)

    def switched(self, name: str, label: str) -> None:
        """A paired computer switched this computer's model."""
        with self._lock:
            self._event("switched", name, label=label)

    def renamed(self, device_id: str, name: str) -> None:
        """Its counts go by the new name; events already listed keep the old one."""
        with self._lock:
            device = self._devices.get(device_id)
            if device is not None:
                device.name = name

    # ---- reading ----

    def snapshot(self, events: Optional[int] = None) -> dict:
        """Totals, per-device counts, and the newest ``events`` (all by default)."""
        with self._lock:
            devices = {device_id: asdict(device) for device_id, device in self._devices.items()}
            recent = list(self._events)
            since = self.started_at
            serial = self._serial
        recent.reverse()
        if events is not None:
            recent = recent[:max(0, events)]
        snapshot = empty_snapshot(since)
        snapshot["serial"] = serial
        snapshot["devices"] = devices
        snapshot["events"] = recent
        for device in devices.values():
            for key in ("transcriptions", "previews", "errors"):
                snapshot[key] += device[key]
            snapshot["audio_s"] += device["audio_s"]
            snapshot["host_s"] += device["host_s"]
            snapshot["last_at"] = max(snapshot["last_at"], device["last_at"])
        return snapshot
