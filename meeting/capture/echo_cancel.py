"""Meeting Mode acoustic echo cancellation (AEC) bridge.

Reuses OpenWhisper's system-audio echo cancellation pipeline
(``services.echo_cancellation`` — GCC-PHAT lag alignment, headphone detection,
and WebRTC AEC3 via ``pywebrtc-audio``) for Meeting Mode's dual-channel
(``mic`` + ``loopback``) capture.

Two entry points cover both Meeting Mode transcription paths:

* :class:`MeetingEchoCanceller` buffers recent live loopback blocks on the
  meeting clock and cleans each spooled 16 kHz microphone chunk before it is
  written to disk and handed to live ASR (and meeting audio playback).
* :func:`cancel_session_echo` cleans the continuous ``mic_session.wav`` against
  ``loopback_session.wav`` in bounded windows before offline re-decode.

Both entry points are **headphone-safe** (GCC-PHAT skips AEC whenever no
acoustic speaker bleed is detected) and **fail-open** (missing
``pywebrtc-audio`` or any DSP error leaves raw microphone audio untouched).
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: Rolling loopback history kept in memory for live chunk cancellation (seconds).
#: Covers ``DEFAULT_MAX_SEC`` (20 s) plus spool queue latency and lag search margin.
DEFAULT_WINDOW_S = 45.0

#: Safety cap on buffered live loopback PCM bytes (~175 s at 48 kHz mono int16).
DEFAULT_MAX_BYTES = 16 * 1024 * 1024

#: Extra meeting-time margin around each mic chunk when selecting loopback blocks.
CHUNK_CONTEXT_MARGIN_S = 1.0

#: Window duration (seconds) for offline session AEC. Bounds peak memory on
#: multi-hour meetings, re-locks GCC-PHAT against hardware clock drift, and
#: adapts if headphones are plugged or unplugged mid-meeting.
SESSION_AEC_WINDOW_S = 120.0

#: Native 16 kHz sample rate shared by Meeting Mode spools and WebRTC AEC3.
TARGET_SAMPLE_RATE = 16000


class MeetingEchoCanceller:
    """Live per-chunk echo canceller backed by Meeting Mode's loopback stream.

    ``feed_loopback`` is called from the loopback audio callback thread and
    does only an O(1) bounded deque append. ``cancel_chunk`` runs on the
    microphone ``SpoolWriter`` thread when a 5–20 s chunk is cut.
    """

    def __init__(
        self,
        *,
        window_s: float = DEFAULT_WINDOW_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        canceller_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._window_s = max(1.0, float(window_s))
        self._max_bytes = max(1, int(max_bytes))
        self._canceller_factory = canceller_factory
        self._lock = threading.Lock()
        self._blocks: deque[Any] = deque()
        self._bytes = 0
        self._error_logged = False
        self._applied_logged = False

        self.chunks_processed = 0
        self.chunks_applied = 0
        self.chunks_skipped = 0
        self.last_result: Any | None = None

    def feed_loopback(
        self,
        frames: np.ndarray,
        t_meeting: float,
        sample_rate: int,
    ) -> None:
        """Buffer one loopback block on the meeting clock. Never blocks or raises."""
        try:
            if frames is None or len(frames) == 0:
                return
            rate = int(sample_rate)
            if rate <= 0:
                return
            arr = np.ascontiguousarray(frames, dtype=np.int16).reshape(-1).copy()
            nbytes = int(arr.nbytes)
            if nbytes == 0 or nbytes > self._max_bytes:
                return
            from services.echo_cancellation import ReferenceBlock

            block = ReferenceBlock(
                t_mono=float(t_meeting),
                sample_rate=rate,
                frames=arr,
            )
            cutoff = float(t_meeting) - self._window_s
            with self._lock:
                self._blocks.append(block)
                self._bytes += nbytes
                while len(self._blocks) > 1:
                    oldest = self._blocks[0]
                    oldest_end = oldest.t_mono + oldest.frames.size / float(
                        oldest.sample_rate
                    )
                    if oldest_end < cutoff or self._bytes > self._max_bytes:
                        dropped = self._blocks.popleft()
                        self._bytes -= int(dropped.frames.nbytes)
                    else:
                        break
        except Exception:
            if not self._error_logged:
                self._error_logged = True
                logger.warning(
                    "Meeting echo cancellation: failed to buffer loopback block; "
                    "continuing with raw microphone audio",
                    exc_info=True,
                )

    def cancel_chunk(
        self,
        mic_16k: np.ndarray,
        start_s: float,
        *,
        sample_rate: int = TARGET_SAMPLE_RATE,
    ) -> np.ndarray:
        """Strip speaker bleed from one 16 kHz mono int16 mic chunk. Fail-open."""
        mic = np.ascontiguousarray(np.asarray(mic_16k, dtype=np.int16).reshape(-1))
        if mic.size == 0 or int(sample_rate) <= 0:
            return mic

        self.chunks_processed += 1
        rate = int(sample_rate)
        duration_s = mic.size / float(rate)
        win_start = float(start_s) - CHUNK_CONTEXT_MARGIN_S
        win_end = float(start_s) + duration_s + CHUNK_CONTEXT_MARGIN_S

        with self._lock:
            blocks = [
                blk
                for blk in self._blocks
                if (blk.t_mono + blk.frames.size / float(blk.sample_rate)) >= win_start
                and blk.t_mono <= win_end
            ]

        if not blocks:
            self.chunks_skipped += 1
            return mic

        try:
            from services.echo_cancellation import (
                build_aligned_reference_16k,
                cancel_echo,
            )

            far_16k = build_aligned_reference_16k(
                blocks,
                mic.size,
                mic_start_mono=float(start_s),
                pause_spans=(),
                target_rate=rate,
            )
            result = cancel_echo(
                mic,
                far_16k,
                sample_rate=rate,
                canceller_factory=self._canceller_factory,
            )
            self.last_result = result
            if result.applied and result.audio is not None:
                self.chunks_applied += 1
                level = logging.DEBUG if self._applied_logged else logging.INFO
                self._applied_logged = True
                logger.log(
                    level,
                    "Meeting echo cancellation applied at %.1fs: lag=%.1f ms, "
                    "z=%.1f, rms %.1f -> %.1f dBFS (far %.1f dBFS) in %.0f ms",
                    start_s,
                    result.delay_ms,
                    result.correlation_z,
                    result.input_rms_db,
                    result.output_rms_db,
                    result.far_rms_db,
                    result.elapsed_ms,
                )
                return result.audio
            self.chunks_skipped += 1
            logger.debug(
                "Meeting echo cancellation skipped at %.1fs (%s; lag=%.1f ms, "
                "z=%.1f, far=%.1f dBFS)",
                start_s,
                result.reason,
                result.delay_ms,
                result.correlation_z,
                result.far_rms_db,
            )
            return mic
        except Exception:
            self.chunks_skipped += 1
            if not self._error_logged:
                self._error_logged = True
                logger.warning(
                    "Meeting echo cancellation failed on mic chunk at %.1fs; "
                    "keeping raw microphone audio",
                    start_s,
                    exc_info=True,
                )
            else:
                logger.debug(
                    "Meeting echo cancellation failed on mic chunk at %.1fs",
                    start_s,
                    exc_info=True,
                )
            return mic

    def clear(self) -> None:
        """Release all buffered loopback blocks."""
        with self._lock:
            self._blocks.clear()
            self._bytes = 0


def cancel_session_echo(
    mic_frames: np.ndarray,
    mic_origin_s: float,
    loopback_frames: np.ndarray,
    loopback_origin_s: float,
    *,
    sample_rate: int = TARGET_SAMPLE_RATE,
    loopback_sample_rate: int | None = None,
    window_s: float = SESSION_AEC_WINDOW_S,
    canceller_factory: Callable[..., Any] | None = None,
) -> tuple[np.ndarray, bool]:
    """Cancel speaker bleed from a continuous meeting mic session recording.

    Aligns ``loopback_frames`` onto ``mic_frames`` using their meeting-clock
    origins and runs WebRTC AEC3 in bounded windows so long meetings stay
    memory-bounded and immune to hardware clock drift.

    Returns:
        ``(audio, applied)`` where ``applied`` is True when at least one window
        had confirmed speaker echo cancelled. Fails open to ``(mic_frames, False)``.
    """
    mic = np.ascontiguousarray(np.asarray(mic_frames, dtype=np.int16).reshape(-1))
    lb = np.ascontiguousarray(np.asarray(loopback_frames, dtype=np.int16).reshape(-1))
    rate = int(sample_rate)
    if mic.size == 0 or lb.size == 0 or rate <= 0:
        return mic, False

    try:
        from services.echo_cancellation import (
            cancel_echo,
            is_available,
            resample_int16_mono,
        )

        if canceller_factory is None and not is_available():
            return mic, False

        lb_rate = int(loopback_sample_rate or rate)
        if lb_rate != rate:
            lb = resample_int16_mono(lb, lb_rate, rate)
            if lb.size == 0:
                return mic, False

        offset = int(round((float(loopback_origin_s) - float(mic_origin_s)) * float(rate)))
        win_samples = max(rate, int(round(max(1.0, float(window_s)) * float(rate))))
        output: np.ndarray | None = None
        applied_windows = 0
        total_windows = 0

        for win_start in range(0, mic.size, win_samples):
            win_end = min(mic.size, win_start + win_samples)
            win_len = win_end - win_start
            total_windows += 1

            j_start = win_start - offset
            j_end = win_end - offset
            src_start = max(0, j_start)
            src_end = min(lb.size, j_end)
            if src_end <= src_start:
                continue

            far_win = np.zeros(win_len, dtype=np.int16)
            dst_start = src_start - j_start
            copy_n = src_end - src_start
            far_win[dst_start : dst_start + copy_n] = lb[src_start : src_end]

            result = cancel_echo(
                mic[win_start:win_end],
                far_win,
                sample_rate=rate,
                canceller_factory=canceller_factory,
            )
            if result.applied and result.audio is not None:
                if output is None:
                    output = mic.copy()
                output[win_start:win_end] = result.audio
                applied_windows += 1

        if output is not None and applied_windows > 0:
            logger.info(
                "Offline meeting echo cancellation applied on %d/%d window(s)",
                applied_windows,
                total_windows,
            )
            return output, True
        return mic, False
    except Exception:
        logger.warning(
            "Offline meeting echo cancellation failed; keeping raw microphone session",
            exc_info=True,
        )
        return mic, False
