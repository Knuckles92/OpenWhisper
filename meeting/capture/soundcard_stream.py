"""Shared SoundCard loopback capture for Meeting Mode.

Used when PortAudio exposes no WASAPI ``[Loopback]`` input device. On Windows
this is the fallback path; on Linux it is the production system-audio path
through PulseAudio or PipeWire-Pulse monitor sources. A daemon thread records
the default speaker's loopback stream at 48 kHz and emits mono int16
``CaptureBlock`` values with the same surface as ``SdCaptureSource``.

``soundcard`` is imported lazily inside methods so this module (and the
capture package) imports cleanly when the library is not installed.
"""
from __future__ import annotations

import logging
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
#: terminal open failure). Kept short so engine watchdog retries still fit
#: the ≤12s restoration bound (poll + retry spacing + start wait).
_START_TIMEOUT_S = 2.0

#: How long a freshly started stream may go without its first block.
_FIRST_BLOCK_GRACE_S = 2.0

#: How long a running stream may go without a block before it is judged dead.
_STALL_TIMEOUT_S = 3.0


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
                from meeting.capture.linux_audio import resolve_linux_monitor
                selection = resolve_linux_monitor()
                return selection.sink_id == str(self.device_id)
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
        monitor = None
        sink_label = ""
        monitor_label = ""
        server_kind = "unknown"
        try:
            import soundcard as sc

            if sys.platform.startswith("linux"):
                from meeting.capture.linux_audio import resolve_linux_monitor
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
            with monitor.recorder(samplerate=SAMPLERATE, channels=min(2, channels)) as recorder:
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
            # Stamp the first frame: the block just finished being captured.
            t_mono = time.monotonic() - frames.size / float(SAMPLERATE)
            self._last_block_mono = time.monotonic()
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
