"""Primary capture source: sounddevice (PortAudio) input streams.

One ``SdCaptureSource`` wraps one ``sd.InputStream`` — either the microphone
or a WASAPI ``[Loopback]`` input device — and delivers mono int16
``CaptureBlock`` values at the device's native sample rate. Downmixing
happens inside the audio callback; resampling is the spool's job.

A failed open raises out of ``start()`` so the engine can fall back to
another source instead of silently recording nothing; a mid-stream abort
marks the source inactive and logs, leaving recovery to the engine's
watchdog.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import numpy as np

from meeting.interfaces import CaptureBlock

logger = logging.getLogger(__name__)

#: Frames per PortAudio callback.
BLOCKSIZE = 1024

#: Full tracebacks are logged for at most this many callback errors.
_MAX_LOGGED_CALLBACK_ERRORS = 5


class SdCaptureSource:
    """A single sounddevice input stream implementing ``CaptureSource``."""

    def __init__(self, channel: str, device_index: int, samplerate: int,
                 n_channels: int) -> None:
        self.channel = channel
        self._device_index = int(device_index)
        self._samplerate = int(samplerate)
        self._n_channels = max(1, int(n_channels))
        self._on_block: Optional[Callable[[CaptureBlock], None]] = None
        self._stream = None
        self._active = False
        self._status_logged = False
        self._callback_errors = 0

    @property
    def device_id(self) -> int:
        """Stable sounddevice index used by the capture watchdog."""
        return self._device_index

    def start(self, on_block: Callable[[CaptureBlock], None]) -> None:
        """Open the stream and begin delivering blocks to ``on_block``.

        Args:
            on_block: Called from the audio thread with each ``CaptureBlock``;
                must be fast and must never raise.

        Raises:
            Exception: Whatever PortAudio raised when the device could not be
                opened or started. Callers must treat a raise as "this
                channel is dead" -- swallowing it here once let a failed mic
                look like a healthy one that simply never spoke.
        """
        self._on_block = on_block
        try:
            import sounddevice as sd
            self._stream = sd.InputStream(
                device=self._device_index,
                samplerate=self._samplerate,
                channels=self._n_channels,
                dtype="int16",
                blocksize=BLOCKSIZE,
                callback=self._callback,
                finished_callback=self._on_finished,
            )
            self._stream.start()
            self._active = True
            logger.info(
                "Capture started: channel=%s device=%d rate=%d channels=%d",
                self.channel, self._device_index, self._samplerate,
                self._n_channels,
            )
        except Exception:
            logger.exception(
                "Failed to start %s capture on device %d",
                self.channel, self._device_index,
            )
            self._active = False
            self._close_stream()
            raise

    def stop(self) -> None:
        """Stop the stream and release the device."""
        self._active = False
        self._close_stream()
        logger.info("Capture stopped: channel=%s", self.channel)

    def is_active(self) -> bool:
        """True while the underlying stream is delivering frames."""
        if not self._active or self._stream is None:
            return False
        try:
            return bool(self._stream.active)
        except Exception:
            return False

    def _callback(self, indata, frames, time_info, status) -> None:
        try:
            if status and not self._status_logged:
                self._status_logged = True
                logger.warning("Capture stream status (%s): %s",
                               self.channel, status)

            t_mono = time.monotonic()
            try:
                adc = float(time_info.inputBufferAdcTime)
                stream = self._stream
                now_pa = float(stream.time) if stream is not None else 0.0
                if adc > 0.0 and now_pa >= adc:
                    # Shift back by PortAudio's buffering latency so t_mono
                    # approximates the capture instant of the first frame.
                    t_mono -= (now_pa - adc)
            except Exception:
                pass  # keep the plain monotonic stamp

            if indata.ndim == 2 and indata.shape[1] > 1:
                mono = indata.astype(np.int32).mean(axis=1).astype(np.int16)
            else:
                # Copy: PortAudio reuses the buffer after the callback returns.
                mono = np.asarray(indata, dtype=np.int16).reshape(-1).copy()

            on_block = self._on_block
            if on_block is not None and self._active:
                on_block(CaptureBlock(
                    channel=self.channel,
                    frames=mono,
                    sample_rate=self._samplerate,
                    t_mono=t_mono,
                ))
        except Exception:
            self._callback_errors += 1
            if self._callback_errors <= _MAX_LOGGED_CALLBACK_ERRORS:
                logger.exception("Capture callback error (%s)", self.channel)

    def _on_finished(self) -> None:
        if self._active:
            self._active = False
            logger.warning("Capture stream ended unexpectedly: channel=%s",
                           self.channel)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            logger.debug("Stream stop failed (%s)", self.channel, exc_info=True)
        try:
            stream.close()
        except Exception:
            logger.debug("Stream close failed (%s)", self.channel, exc_info=True)
