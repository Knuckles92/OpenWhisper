"""Fallback loopback capture using the ``soundcard`` library.

Used when the bundled PortAudio build exposes no WASAPI ``[Loopback]`` input
devices. A daemon thread records the default speaker's loopback stream at
48 kHz and emits mono int16 ``CaptureBlock`` values with the same surface as
``SdCaptureSource``.

``soundcard`` is imported lazily inside methods so this module (and the
capture package) imports cleanly when the library is not installed.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

from meeting.interfaces import CHANNEL_LOOPBACK, CaptureBlock

logger = logging.getLogger(__name__)

#: Capture sample rate requested from soundcard.
SAMPLERATE = 48000

#: Frames per read from the recorder.
BLOCK_FRAMES = 1024

#: Full tracebacks are logged for at most this many callback errors.
_MAX_LOGGED_CALLBACK_ERRORS = 5

#: ``start()`` waits at most this long for the recorder thread to open (or
#: fail to open) the loopback device, so callers can trust ``is_active()``.
_START_TIMEOUT_S = 1.0


class SoundcardLoopbackSource:
    """Loopback ``CaptureSource`` backed by the ``soundcard`` library."""

    def __init__(self) -> None:
        self.channel = CHANNEL_LOOPBACK
        self.device_id = "soundcard-default"
        self._on_block: Optional[Callable[[CaptureBlock], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._active = False
        self._callback_errors = 0
        self._settled = threading.Event()

    @staticmethod
    def available() -> bool:
        """True when the ``soundcard`` library can be imported."""
        try:
            import soundcard  # noqa: F401
            return True
        except Exception:
            return False

    def start(self, on_block: Callable[[CaptureBlock], None]) -> None:
        """Start the recorder thread delivering blocks to ``on_block``.

        Returns only once the recorder thread has opened the loopback device
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
        self._settled.clear()
        self._thread = threading.Thread(
            target=self._run, name="meeting-sc-loopback", daemon=True
        )
        self._thread.start()
        if not self._settled.wait(_START_TIMEOUT_S):
            logger.warning("Soundcard loopback device did not open within "
                           "%.1fs", _START_TIMEOUT_S)

    def stop(self) -> None:
        """Stop the recorder thread and release the device."""
        self._running = False
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._active = False
        logger.info("Soundcard loopback capture stopped")

    def is_active(self) -> bool:
        """True while the recorder thread is delivering frames."""
        thread = self._thread
        return self._active and thread is not None and thread.is_alive()

    def is_default_device_current(self) -> bool:
        """Whether this fallback still records the current default speaker."""
        try:
            import soundcard as sc
            return str(sc.default_speaker().name) == self.device_id
        except Exception:
            logger.debug("Could not probe the default soundcard speaker",
                         exc_info=True)
            return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Recorder loop: read float32 blocks, convert, emit."""
        _coinitialize()
        try:
            import soundcard as sc
            speaker = sc.default_speaker()
            self.device_id = str(speaker.name)
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        except Exception:
            logger.exception("Soundcard loopback device unavailable")
            self._running = False
            self._settled.set()
            return
        try:
            with mic.recorder(samplerate=SAMPLERATE) as recorder:
                self._active = True
                self._settled.set()
                logger.info("Soundcard loopback capture started (device: %s)",
                            speaker.name)
                while self._running:
                    data = recorder.record(numframes=BLOCK_FRAMES)
                    if data is None or len(data) == 0:
                        continue
                    self._emit(data)
        except Exception:
            logger.exception("Soundcard loopback capture failed")
        finally:
            self._active = False
            self._settled.set()

    def _emit(self, data) -> None:
        """Convert one float32 block to mono int16 and deliver it."""
        try:
            arr = np.asarray(data, dtype=np.float32)
            if arr.ndim == 2 and arr.shape[1] > 1:
                arr = arr.mean(axis=1)
            else:
                arr = arr.reshape(-1)
            frames = np.clip(arr * 32767.0, -32768.0, 32767.0).astype(np.int16)
            # Stamp the first frame: the block just finished being captured.
            t_mono = time.monotonic() - frames.size / float(SAMPLERATE)
            on_block = self._on_block
            if on_block is not None:
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
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    except Exception:
        pass  # non-Windows, or COM already initialized on this thread
