"""Meeting clock: a pausable monotonic timeline shared by both capture streams.

Every timestamp in Meeting Mode (chunk offsets, segment times, evidence
anchors) is expressed in *meeting seconds*: seconds since the meeting started,
excluding paused time. Anchoring on ``time.monotonic()`` makes the timeline
immune to wall-clock adjustments; both capture streams timestamp against the
same clock so cross-stream interleaving stays consistent.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from meeting.time_utils import utc_now_iso


class MeetingClock:
    """Pausable monotonic meeting timeline.

    Usage: ``start()`` once, then convert audio-thread timestamps with
    ``meeting_time(t_mono)`` or read the current offset with ``now_s()``.
    ``pause()``/``resume()`` accumulate pause credit so meeting time stands
    still while paused.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._t0: Optional[float] = None
        self._pause_credit = 0.0
        self._paused_at: Optional[float] = None
        self.started_at_iso: Optional[str] = None

    def start(self) -> None:
        """Anchor the meeting epoch at the current monotonic instant."""
        with self._lock:
            self._t0 = time.monotonic()
            self._pause_credit = 0.0
            self._paused_at = None
            self.started_at_iso = utc_now_iso()

    def resume_from_recovery(self, elapsed_s: float) -> None:
        """Re-anchor after crash recovery so meeting time continues at ``elapsed_s``.

        Args:
            elapsed_s: Meeting seconds already recorded before the crash; the
                downtime gap is treated as pause credit.
        """
        with self._lock:
            self._t0 = time.monotonic() - elapsed_s
            self._pause_credit = 0.0
            self._paused_at = None
            if self.started_at_iso is None:
                self.started_at_iso = utc_now_iso()

    def pause(self) -> None:
        """Freeze meeting time. Idempotent."""
        with self._lock:
            if self._t0 is not None and self._paused_at is None:
                self._paused_at = time.monotonic()

    def resume(self) -> None:
        """Unfreeze meeting time, crediting the paused span. Idempotent."""
        with self._lock:
            if self._paused_at is not None:
                self._pause_credit += time.monotonic() - self._paused_at
                self._paused_at = None

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused_at is not None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._t0 is not None

    def now_s(self) -> float:
        """Current meeting time in seconds (0.0 before ``start()``)."""
        return self.meeting_time(time.monotonic())

    def meeting_time(self, t_mono: float) -> float:
        """Convert a ``time.monotonic()`` timestamp to meeting seconds.

        Timestamps taken while paused resolve to the pause instant, so audio
        blocks that race a pause land at a stable position.
        """
        with self._lock:
            if self._t0 is None:
                return 0.0
            effective = min(t_mono, self._paused_at) if self._paused_at is not None else t_mono
            return max(0.0, effective - self._t0 - self._pause_credit)

    def paused_total_s(self) -> float:
        """Total accumulated pause credit in seconds."""
        with self._lock:
            credit = self._pause_credit
            if self._paused_at is not None:
                credit += time.monotonic() - self._paused_at
            return credit
