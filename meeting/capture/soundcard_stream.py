"""Shared SoundCard loopback capture for Meeting Mode.

Used when PortAudio exposes no WASAPI ``[Loopback]`` input device. On Windows
this is the fallback path; on Linux it is the production system-audio path
through PulseAudio or PipeWire-Pulse monitor sources. A daemon thread records
the default speaker's loopback stream at 48 kHz and emits mono int16
``CaptureBlock`` values with the same surface as ``SdCaptureSource``.

On Linux, SoundCard is reached only through ``meeting.capture.linux_audio``,
which reconnects after audio-server restarts and pins the record stream to
the chosen monitor. Linux blocks are stamped by sample count
(:class:`_SampleClock`): PulseAudio delivers monitor audio in fragments, and
stamping each block at delivery collapses a fragment's blocks onto one
instant, which the spool then trims as overlap.

``soundcard`` is imported lazily inside methods so this module (and the
capture package) imports cleanly when the library is not installed.
"""
from __future__ import annotations

import logging
import math
import sys
import threading
import time
from typing import Any, Callable, Optional

import numpy as np

from meeting.interfaces import CHANNEL_LOOPBACK, CaptureBlock

logger = logging.getLogger(__name__)

#: Capture sample rate requested from soundcard.
SAMPLERATE = 48000

#: Frames per read from the recorder.
BLOCK_FRAMES = 1024

#: Full tracebacks are logged for at most this many callback errors.
_MAX_LOGGED_CALLBACK_ERRORS = 5

#: ``start()`` waits at most this long for the first valid block (or a
#: terminal open failure). An idle PulseAudio sink runs at its maximum
#: latency until a stream asks for less, so its monitor's first fragment can
#: take about 2s (measured on PulseAudio 17). Engine watchdog retries still
#: fit the ≤12s restoration bound (poll + retry spacing + start wait).
_START_TIMEOUT_S = 4.0

#: How long a freshly started stream may go without its first block.
_FIRST_BLOCK_GRACE_S = _START_TIMEOUT_S

#: How long a running stream may go without a block before it is judged dead.
_STALL_TIMEOUT_S = 3.0

#: Sample-clock window. Must exceed the longest delivery burst so that it
#: always holds the least-delayed block.
_CLOCK_WINDOW_S = 2.0

#: A lag every block in a window shares beyond this means frames were lost
#: upstream; the clock skips ahead and the spool fills the gap with silence.
_CLOCK_RESYNC_S = 0.15

#: A single lag this large is a dropout on its own; skip ahead at once.
_CLOCK_JUMP_S = 2.5


class _SampleClock:
    """Stamps blocks by sample count instead of by delivery time.

    The stream origin is the tightest bound the deliveries allow: a block
    cannot arrive before its last frame was captured, so
    ``now - frames_through_block / rate`` bounds the origin from above, and
    the minimum over blocks settles on the least-delayed delivery. Blocks are
    then contiguous no matter how the backend batches them.
    """

    def __init__(self, rate: int) -> None:
        self._rate = float(rate)
        self._origin: Optional[float] = None
        self._frames = 0
        self._window_start = 0.0
        self._window_min_lag = math.inf

    def stamp(self, n_frames: int, now: float) -> float:
        """Monotonic time of the first of ``n_frames`` delivered at ``now``."""
        end = self._frames + int(n_frames)
        bound = now - end / self._rate
        if self._origin is None or bound < self._origin:
            if self._origin is None:
                self._window_start = now
            self._origin = bound
        lag = bound - self._origin
        if lag >= _CLOCK_JUMP_S:
            self._origin = bound
            self._window_start = now
            self._window_min_lag = math.inf
        else:
            self._window_min_lag = min(self._window_min_lag, lag)
            if now - self._window_start >= _CLOCK_WINDOW_S:
                if self._window_min_lag > _CLOCK_RESYNC_S:
                    self._origin += self._window_min_lag
                self._window_start = now
                self._window_min_lag = math.inf
        stamp = self._origin + self._frames / self._rate
        self._frames = end
        return stamp


class SoundcardLoopbackSource:
    """Loopback ``CaptureSource`` backed by the ``soundcard`` library."""

    def __init__(self, selection: Optional[Any] = None) -> None:
        self.channel = CHANNEL_LOOPBACK
        self.device_id = "soundcard-default"
        self._selection = selection
        self._on_block: Optional[Callable[[CaptureBlock], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._recorder_opened = False
        self._active = False
        self._start_mono = 0.0
        self._last_block_mono = 0.0
        self._callback_errors = 0
        self._settled = threading.Event()
        self._failure: Optional[str] = None
        self._clock: Optional[_SampleClock] = None

    @staticmethod
    def available() -> bool:
        """True when SoundCard can capture the default output monitor now."""
        if sys.platform.startswith("linux"):
            try:
                from meeting.capture.linux_audio import probe_linux_audio
                return bool(probe_linux_audio(verify_open=False).ready)
            except Exception:
                return False
        try:
            import soundcard  # noqa: F401
            return True
        except Exception:
            return False

    def start(self, on_block: Callable[[CaptureBlock], None]) -> None:
        """Start the recorder thread delivering blocks to ``on_block``.

        Returns only once the recorder thread has delivered its first block
        or failed trying (bounded by ``_START_TIMEOUT_S``), so a caller that
        checks ``is_active()`` afterwards learns the truth instead of seeing
        a not-yet-probed source that will never produce audio.

        Args:
            on_block: Called from the recorder thread with each
                ``CaptureBlock``; must be fast and must never raise.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Soundcard loopback capture already running")
            return
        self._on_block = on_block
        self._running = True
        self._recorder_opened = False
        self._active = False
        self._start_mono = time.monotonic()
        self._last_block_mono = 0.0
        self._callback_errors = 0
        self._failure = None
        # The fragment bursts were measured on PulseAudio; the Windows
        # fallback keeps its delivery-time stamps.
        self._clock = (
            _SampleClock(SAMPLERATE) if sys.platform.startswith("linux")
            else None
        )
        self._settled.clear()
        self._thread = threading.Thread(
            target=self._run, name="meeting-sc-loopback", daemon=True
        )
        self._thread.start()
        if not self._settled.wait(_START_TIMEOUT_S):
            logger.warning(
                "Soundcard loopback did not deliver audio within %.1fs",
                _START_TIMEOUT_S,
            )
            self._failure = self._failure or "start_timeout"
            self._running = False

    def stop(self) -> None:
        """Stop the recorder thread and release the device."""
        self._running = False
        self._on_block = None
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._active = False
        self._recorder_opened = False
        logger.info("Soundcard loopback capture stopped")

    def is_active(self) -> bool:
        """True while the recorder thread is delivering frames."""
        thread = self._thread
        if not self._running or thread is None or not thread.is_alive():
            return False
        if self._failure:
            return False
        if self._last_block_mono <= 0.0:
            return (time.monotonic() - self._start_mono) < _FIRST_BLOCK_GRACE_S
        return (time.monotonic() - self._last_block_mono) < _STALL_TIMEOUT_S

    def is_default_device_current(self) -> bool:
        """Whether this source still records the current default speaker."""
        try:
            if sys.platform.startswith("linux"):
                # Polled every watchdog tick: one server query, no monitor
                # resolution and no pactl/systemctl subprocesses.
                from meeting.capture.linux_audio import default_sink_id
                return default_sink_id() == str(self.device_id)
            import soundcard as sc
            speaker = sc.default_speaker()
            speaker_id = str(getattr(speaker, "id", None) or speaker.name)
            return speaker_id == str(self.device_id)
        except Exception:
            logger.debug(
                "Could not probe the default soundcard speaker",
                exc_info=True,
            )
            # Fail closed so the watchdog can recover.
            return False

    def _run(self) -> None:
        if sys.platform.startswith("win"):
            _coinitialize()
        linux = sys.platform.startswith("linux")
        monitor = None
        sc: Any = None
        sink_label = ""
        monitor_label = ""
        server_kind = "unknown"
        try:
            if linux:
                from meeting.capture.linux_audio import (
                    load_soundcard,
                    resolve_linux_monitor,
                )
                sc = load_soundcard()
                selection = self._selection or resolve_linux_monitor(sc)
                self.device_id = selection.sink_id
                sink_label = selection.sink_name or selection.sink_id
                monitor_label = selection.monitor_name or selection.monitor_id
                server_kind = selection.server_kind
                lookup_id = (
                    getattr(selection, "soundcard_id", None)
                    or selection.monitor_id
                    or selection.sink_id
                )
                try:
                    monitor = sc.get_microphone(
                        id=lookup_id, include_loopback=True
                    )
                except Exception:
                    monitor = None
                if monitor is None and lookup_id != selection.monitor_id:
                    try:
                        monitor = sc.get_microphone(
                            id=selection.monitor_id, include_loopback=True
                        )
                    except Exception:
                        monitor = None
                if monitor is None or not bool(
                    getattr(monitor, "isloopback", False)
                ):
                    raise RuntimeError("selected Linux monitor is not loopback")
                monitor_identity = str(
                    getattr(monitor, "id", None)
                    or getattr(monitor, "name", "")
                    or ""
                )
                if monitor_identity not in {
                    str(selection.monitor_id),
                    str(selection.sink_id),
                    str(lookup_id),
                }:
                    raise RuntimeError("selected Linux monitor identity mismatch")
            else:
                import soundcard as sc

                speaker = sc.default_speaker()
                self.device_id = str(getattr(speaker, "id", None) or speaker.name)
                sink_label = str(speaker.name)
                monitor = sc.get_microphone(
                    id=str(speaker.name), include_loopback=True
                )
                monitor_label = str(getattr(monitor, "name", monitor))
                server_kind = "wasapi"
        except Exception:
            logger.exception("Soundcard loopback device unavailable")
            self._failure = "device_unavailable"
            self._running = False
            self._settled.set()
            return

        try:
            channels = max(1, int(getattr(monitor, "channels", 2) or 2))
            if linux:
                from meeting.capture.linux_audio import open_monitor_recorder
                recorder_cm = open_monitor_recorder(
                    sc, monitor, samplerate=SAMPLERATE,
                    channels=min(2, channels),
                )
            else:
                recorder_cm = monitor.recorder(
                    samplerate=SAMPLERATE, channels=min(2, channels)
                )
            with recorder_cm as recorder:
                self._recorder_opened = True
                logger.info(
                    "Soundcard loopback capture opened "
                    "(server=%s sink=%s monitor=%s rate=%d channels=%d)",
                    server_kind, sink_label, monitor_label, SAMPLERATE, channels,
                )
                while self._running:
                    data = recorder.record(numframes=BLOCK_FRAMES)
                    if data is None or len(data) == 0:
                        continue
                    self._emit(data)
        except Exception:
            logger.exception("Soundcard loopback capture failed")
            self._failure = self._failure or "recorder_failed"
        finally:
            self._active = False
            self._recorder_opened = False
            self._settled.set()

    def _emit(self, data) -> None:
        if not self._running:
            return
        try:
            arr = np.asarray(data, dtype=np.float32)
            if arr.ndim == 2 and arr.shape[1] > 1:
                arr = arr.mean(axis=1)
            else:
                arr = arr.reshape(-1)
            frames = np.clip(arr * 32767.0, -32768.0, 32767.0).astype(np.int16)
            if frames.size <= 0:
                return
            now = time.monotonic()
            clock = self._clock
            if clock is not None:
                t_mono = clock.stamp(frames.size, now)
            else:
                # The block just finished being captured.
                t_mono = now - frames.size / float(SAMPLERATE)
            self._last_block_mono = now
            if not self._active:
                self._active = True
                self._settled.set()
                logger.info(
                    "Soundcard loopback first audio block: %d frames @ %d Hz",
                    frames.size, SAMPLERATE,
                )
            on_block = self._on_block
            if on_block is not None and self._running:
                on_block(CaptureBlock(
                    channel=self.channel,
                    frames=frames,
                    sample_rate=SAMPLERATE,
                    t_mono=t_mono,
                ))
        except Exception:
            self._callback_errors += 1
            if self._callback_errors <= _MAX_LOGGED_CALLBACK_ERRORS:
                logger.exception("Soundcard loopback block delivery failed")


def _coinitialize() -> None:
    """Best-effort per-thread COM initialization (Windows only).

    ``soundcard`` initializes COM when first imported, which happens on the
    recorder thread here; this guards restarts where the import is already
    cached but the new thread has no COM apartment.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    except Exception:
        pass  # COM already initialized on this thread
