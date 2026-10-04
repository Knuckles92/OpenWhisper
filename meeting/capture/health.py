"""Lightweight per-channel health observations for live capture.

An open stream is not proof that it is delivering buffers or useful signal.
This monitor records callback arrival and counts sustained signal windows; it
never declares quiet audio faulty, since a person or system output may simply
be silent. Signal counters let the dashboard run an explicit, guided check.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Dict

import numpy as np

from meeting.interfaces import CaptureBlock

# The spool's existing int16 quiet threshold had no misses on 9,059 annotated
# 3-second speech-rich windows from ten AMI headset-mix meetings. It is a
# *guided-check* threshold, not a passive silence alarm or proof of source ID.
SIGNAL_RMS_INT16 = 300.0
SIGNAL_WINDOW_S = 0.25
MIN_CONSECUTIVE_SIGNAL_WINDOWS = 2
SIGNAL_REPORT_INTERVAL_S = 2.0
FIRST_BLOCK_GRACE_S = 4.0
BLOCK_STALL_S = 3.0


class CaptureSignalMonitor:
    """Observe one capture channel without blocking the audio callback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.signal_windows = 0
        self._generation = 0
        self.reset_stream()

    def reset_stream(self, now: float | None = None) -> None:
        """Start a new source generation while preserving the signal counter."""
        with self._lock:
            self._generation += 1
            self.started_mono = time.monotonic() if now is None else now
            self.last_block_mono: float | None = None
            self._sum_squares = 0.0
            self._samples = 0
            self._sample_rate = 0
            self._consecutive_signal_windows = 0
            self._last_reported_signal_mono: float | None = None

    def observe(self, block: CaptureBlock, *, signal_enabled: bool = True,
                now: float | None = None) -> None:
        """Record callback delivery and one 0.25-second RMS window at a time."""
        receipt = time.monotonic() if now is None else now
        frames = np.asarray(block.frames, dtype=np.int16)
        if frames.size == 0:
            return
        rate = max(1, int(block.sample_rate))
        # Calculate outside the lock to keep the callback's critical section
        # short even while the watchdog snapshots this monitor.
        squares = 0.0
        if signal_enabled:
            data = frames.astype(np.float64, copy=False)
            squares = float(np.dot(data, data))
        with self._lock:
            self.last_block_mono = receipt
            if not signal_enabled:
                self._sum_squares = 0.0
                self._samples = 0
                self._consecutive_signal_windows = 0
                return
            if self._sample_rate != rate:
                self._sum_squares = 0.0
                self._samples = 0
                self._consecutive_signal_windows = 0
                self._sample_rate = rate
            self._sum_squares += squares
            self._samples += int(frames.size)
            if self._samples >= max(1, int(round(rate * SIGNAL_WINDOW_S))):
                rms = math.sqrt(self._sum_squares / self._samples)
                if rms >= SIGNAL_RMS_INT16:
                    self._consecutive_signal_windows += 1
                    if (self._consecutive_signal_windows >= MIN_CONSECUTIVE_SIGNAL_WINDOWS
                            and (self._last_reported_signal_mono is None
                                 or receipt - self._last_reported_signal_mono
                                 >= SIGNAL_REPORT_INTERVAL_S)):
                        # The dashboard needs only a fresh signal pulse. Cap
                        # persistence and status broadcasts during speech.
                        self.signal_windows += 1
                        self._last_reported_signal_mono = receipt
                else:
                    self._consecutive_signal_windows = 0
                self._sum_squares = 0.0
                self._samples = 0

    def snapshot(self, now: float | None = None) -> Dict[str, Any]:
        """Return the minimal state required by watchdog and guided checks."""
        instant = time.monotonic() if now is None else now
        with self._lock:
            last = self.last_block_mono
            started = self.started_mono
            signals = self.signal_windows
            generation = self._generation
        age = instant - (last if last is not None else started)
        return {
            "receiving": last is not None and age < BLOCK_STALL_S,
            "stalled": age >= (BLOCK_STALL_S if last is not None else FIRST_BLOCK_GRACE_S),
            "signal_windows": signals,
            "generation": generation,
        }
