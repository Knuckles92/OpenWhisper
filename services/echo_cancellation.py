"""Beta system-audio acoustic echo cancellation (AEC) for dictation.

Captures system audio (loopback) in parallel with the microphone during a
dictation recording and strips speaker bleed from the saved WAV before
transcription using WebRTC AEC3 (``pywebrtc-audio``).

Design goals:
* **Zero cost when off** (default): ``pywebrtc_audio`` and ``meeting.capture``
  are imported lazily only when the user enables the beta setting.
* **Non-blocking start & fail-open**: loopback opens on a background thread
  and any driver, permission, or DSP failure leaves the raw microphone WAV
  untouched so dictation never fails because of beta AEC.
* **Headphones-safe**: if system audio is silent, or if GCC-PHAT finds no
  acoustic correlation between loopback and microphone (e.g. headphones or
  muted speakers), AEC is skipped so clean speech is never attenuated.
* **Offline alignment + warm-up**: GCC-PHAT estimates the exact acoustic lag
  between the two independent OS audio streams, shifts the reference to a
  small causal margin, warms up AEC3's room filter on a short slice, flushes
  with silence, and cancels the full recording from sample zero.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from typing import Any

import numpy as np

from config import config
from services import audio_devices
from services.wav_metadata import append_origination

logger = logging.getLogger(__name__)

#: Native sample rate fed to WebRTC AEC3 and Whisper/Parakeet.
AEC_SAMPLE_RATE = 16000
#: Causal headroom left in the aligned far-end reference before AEC3 (ms).
AEC_HEADROOM_MS = 15
#: Minimum far-end RMS level (dBFS) before AEC runs; quieter means no media.
MIN_FAR_RMS_DBFS = -65.0
#: Minimum GCC-PHAT peak z-score required to confirm real speaker bleed into
#: the microphone (protects headphone users from unnecessary attenuation).
MIN_ECHO_CORRELATION_Z = 12.0
#: Search window for acoustic/driver lag between loopback and microphone (ms).
LAG_SEARCH_MIN_MS = -250.0
LAG_SEARCH_MAX_MS = 500.0
#: Maximum analysis window for GCC-PHAT delay estimation (seconds).
LAG_ANALYSIS_MAX_S = 15.0
#: Warm-up pass duration (seconds) so short dictations cancel from t=0.
WARMUP_MAX_S = 4.0
#: Silence flushed between warm-up and real pass to clear STFT overlap buffers.
WARMUP_FLUSH_MS = 100
#: Gaps larger than this (seconds) when stitching loopback blocks insert silence.
GAP_TOLERANCE_S = 0.12
#: Safety cap on buffered loopback PCM bytes (~23 min at 48 kHz mono int16).
MAX_REFERENCE_BYTES = 128 * 1024 * 1024
#: Maximum time ``stop()`` waits for the background loopback starter thread.
STOP_JOIN_TIMEOUT_S = 1.5


@dataclass(frozen=True)
class ReferenceBlock:
    """One mono int16 system-audio block with its monotonic start timestamp."""

    t_mono: float
    sample_rate: int
    frames: np.ndarray


@dataclass(frozen=True)
class CapturedReference:
    """System-audio blocks collected during one recording session."""

    blocks: tuple[ReferenceBlock, ...]
    backend: str = ""
    overflowed: bool = False


@dataclass(frozen=True)
class EchoCancellationResult:
    """Outcome and diagnostics of one offline echo-cancellation pass."""

    audio: np.ndarray | None
    applied: bool
    reason: str
    delay_ms: float = 0.0
    correlation_z: float = 0.0
    input_rms_db: float = -120.0
    output_rms_db: float = -120.0
    far_rms_db: float = -120.0
    elapsed_ms: float = 0.0


def is_available() -> bool:
    """Return True when ``pywebrtc_audio.EchoCanceller`` can be imported."""
    try:
        from pywebrtc_audio import EchoCanceller  # noqa: F401
        return True
    except Exception:
        return False


def rms_dbfs(samples: np.ndarray) -> float:
    """RMS level in dBFS for float32 ``[-1, 1]`` or int16 samples."""
    arr = np.asarray(samples).reshape(-1)
    if arr.size == 0:
        return -120.0
    if np.issubdtype(arr.dtype, np.integer):
        scale = float(-np.iinfo(arr.dtype).min)
    else:
        scale = 1.0
    vals = arr.astype(np.float64) / scale
    mean_sq = float(np.dot(vals, vals)) / vals.size
    if mean_sq <= 0.0:
        return -120.0
    return max(-120.0, 10.0 * math.log10(mean_sq))


def resample_int16_mono(samples: np.ndarray, src_rate: int, dst_rate: int = AEC_SAMPLE_RATE) -> np.ndarray:
    """Resample 1-D mono int16 PCM cleanly using PyAV (FFmpeg swresample)."""
    arr = np.ascontiguousarray(np.asarray(samples, dtype=np.int16).reshape(-1))
    if arr.size == 0 or int(src_rate) == int(dst_rate):
        return arr.copy()
    import av

    resampler = av.AudioResampler(format="s16", layout="mono", rate=int(dst_rate))
    frame = av.AudioFrame.from_ndarray(arr.reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = int(src_rate)
    frame.time_base = Fraction(1, int(src_rate))
    frame.pts = None
    pieces: list[np.ndarray] = []
    for converted in resampler.resample(frame):
        chunk = converted.to_ndarray().reshape(-1)
        if chunk.size:
            pieces.append(chunk)
    for converted in resampler.resample(None):
        chunk = converted.to_ndarray().reshape(-1)
        if chunk.size:
            pieces.append(chunk)
    if not pieces:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(pieces).astype(np.int16, copy=False)


def _select_active_window(
    near: np.ndarray, far: np.ndarray, sample_rate: int, max_s: float = LAG_ANALYSIS_MAX_S
) -> tuple[np.ndarray, np.ndarray]:
    """Return the loudest active far-end slice (up to ``max_s``) for lag estimation."""
    n = min(len(near), len(far))
    win = int(round(max_s * sample_rate))
    if n <= win:
        return near[:n], far[:n]
    # Pick the 1-second-step window with maximum far-end energy so silent intros
    # on a long recording do not hide the echo path.
    step = max(1, sample_rate)
    best_start = 0
    best_energy = -1.0
    far_sq = far[:n].astype(np.float64) ** 2
    prefix = np.empty(n + 1, dtype=np.float64)
    prefix[0] = 0.0
    np.cumsum(far_sq, out=prefix[1:])
    for start in range(0, n - win + 1, step):
        energy = float(prefix[start + win] - prefix[start])
        if energy > best_energy:
            best_energy = energy
            best_start = start
    return near[best_start : best_start + win], far[best_start : best_start + win]


def estimate_echo_lag(
    near: np.ndarray,
    far: np.ndarray,
    sample_rate: int = AEC_SAMPLE_RATE,
    *,
    min_ms: float = LAG_SEARCH_MIN_MS,
    max_ms: float = LAG_SEARCH_MAX_MS,
) -> tuple[int, float]:
    """Estimate acoustic lag ``(lag_samples, confidence_z)`` via GCC-PHAT.

    A positive ``lag_samples`` means the echo appears in ``near`` after ``far``
    (``near[t]`` correlates with ``far[t - lag]``). A high ``confidence_z``
    confirms that loudspeaker audio actually bled into the microphone.
    """
    near_win, far_win = _select_active_window(near, far, sample_rate)
    n = min(len(near_win), len(far_win))
    if n < max(256, sample_rate // 4):
        return 0, 0.0

    x = near_win[:n].astype(np.float64)
    y = far_win[:n].astype(np.float64)
    x = x - float(np.mean(x))
    y = y - float(np.mean(y))
    if float(np.dot(x, x)) <= 1e-12 or float(np.dot(y, y)) <= 1e-12:
        return 0, 0.0

    nfft = 1 << int(math.ceil(math.log2(2 * n)))
    X = np.fft.rfft(x, n=nfft)
    Y = np.fft.rfft(y, n=nfft)
    cross = X * np.conj(Y)
    mag = np.abs(cross)
    max_mag = float(np.max(mag))
    if max_mag <= 1e-12:
        return 0, 0.0
    floor = 1e-6 * max_mag + 1e-12
    cc = np.fft.irfft(cross / np.maximum(mag, floor), n=nfft)

    min_lag = max(-(n - 1), int(round(min_ms * sample_rate / 1000.0)))
    max_lag = min(n - 1, int(round(max_ms * sample_rate / 1000.0)))
    if min_lag > max_lag:
        return 0, 0.0

    neg = cc[nfft + min_lag :] if min_lag < 0 else np.empty(0, dtype=np.float64)
    pos = cc[: max_lag + 1] if max_lag >= 0 else np.empty(0, dtype=np.float64)
    if min_lag > 0:
        window = cc[min_lag : max_lag + 1]
    elif max_lag < 0:
        window = cc[nfft + min_lag : nfft + max_lag + 1]
    else:
        window = np.concatenate((neg, pos))

    abs_window = np.abs(window)
    if abs_window.size == 0:
        return 0, 0.0
    peak_idx = int(np.argmax(abs_window))
    peak_val = float(abs_window[peak_idx])
    median = float(np.median(abs_window))
    mad = float(np.median(np.abs(abs_window - median))) + 1e-12
    confidence_z = (peak_val - median) / (1.4826 * mad)
    return min_lag + peak_idx, float(confidence_z)


def align_reference(
    far: np.ndarray,
    length: int,
    lag_samples: int,
    *,
    headroom_samples: int,
) -> np.ndarray:
    """Shift ``far`` so the detected echo sits at ``+headroom_samples`` in ``near``.

    If ``near[t]`` matches ``far[t - lag]``, shifting ``far`` right by
    ``shift = lag - headroom`` places the matching far-end sample at
    ``t - headroom``, ensuring the echo path is strictly causal for AEC3 even
    when OS loopback timestamps lagged behind microphone timestamps.
    """
    out = np.zeros(length, dtype=far.dtype)
    if far.size == 0 or length <= 0:
        return out
    shift = int(lag_samples) - int(headroom_samples)
    if shift >= 0:
        if shift < length:
            copy_n = min(far.size, length - shift)
            out[shift : shift + copy_n] = far[:copy_n]
    else:
        src_start = -shift
        if src_start < far.size:
            copy_n = min(far.size - src_start, length)
            out[:copy_n] = far[src_start : src_start + copy_n]
    return out


def cancel_echo(
    near_16k: np.ndarray,
    far_16k: np.ndarray,
    *,
    sample_rate: int = AEC_SAMPLE_RATE,
    headroom_ms: int = AEC_HEADROOM_MS,
    min_far_rms_dbfs: float = MIN_FAR_RMS_DBFS,
    min_correlation_z: float = MIN_ECHO_CORRELATION_Z,
    canceller_factory: Callable[..., Any] | None = None,
) -> EchoCancellationResult:
    """Cancel loudspeaker echo from 16 kHz mono int16 ``near_16k`` using ``far_16k``."""
    started = time.perf_counter()
    near = np.ascontiguousarray(np.asarray(near_16k, dtype=np.int16).reshape(-1))
    far = np.ascontiguousarray(np.asarray(far_16k, dtype=np.int16).reshape(-1))
    if near.size == 0 or far.size == 0:
        return EchoCancellationResult(None, False, "empty_audio")

    if far.size < near.size:
        far = np.pad(far, (0, near.size - far.size))
    elif far.size > near.size:
        far = far[: near.size]

    near_db = rms_dbfs(near)
    far_db = rms_dbfs(far)
    if far_db < min_far_rms_dbfs:
        return EchoCancellationResult(
            None, False, "no_system_audio",
            input_rms_db=near_db, output_rms_db=near_db, far_rms_db=far_db,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    near_f = near.astype(np.float32) / 32768.0
    far_f = far.astype(np.float32) / 32768.0

    lag_samples, confidence_z = estimate_echo_lag(near_f, far_f, sample_rate)
    delay_ms = lag_samples * 1000.0 / float(sample_rate)
    if confidence_z < min_correlation_z:
        return EchoCancellationResult(
            None, False, "no_echo_detected",
            delay_ms=delay_ms, correlation_z=confidence_z,
            input_rms_db=near_db, output_rms_db=near_db, far_rms_db=far_db,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    headroom_samples = max(0, int(round(headroom_ms * sample_rate / 1000.0)))
    aligned_far = align_reference(
        far_f, near_f.size, lag_samples, headroom_samples=headroom_samples
    )

    if canceller_factory is None:
        from pywebrtc_audio import EchoCanceller
        canceller_factory = EchoCanceller

    canceller = canceller_factory(
        sample_rate=int(sample_rate),
        num_channels=1,
        stream_delay_ms=int(headroom_ms),
    )

    # Warm up the adaptive room filter on the loudest active slice (up to 4 s),
    # then flush STFT overlap buffers with brief silence so the real pass cancels
    # echo cleanly from sample zero without wrap-around clicks.
    warm_near, warm_far = _select_active_window(
        near_f, aligned_far, sample_rate, max_s=WARMUP_MAX_S
    )
    if warm_near.size >= sample_rate // 4:
        canceller.process(
            np.ascontiguousarray(warm_near, dtype=np.float32),
            np.ascontiguousarray(warm_far, dtype=np.float32),
        )
        flush_n = int(round(WARMUP_FLUSH_MS * sample_rate / 1000.0))
        if flush_n > 0:
            zeros = np.zeros(flush_n, dtype=np.float32)
            canceller.process(zeros, zeros)

    cleaned_f = np.asarray(
        canceller.process(
            np.ascontiguousarray(near_f, dtype=np.float32),
            np.ascontiguousarray(aligned_far, dtype=np.float32),
        ),
        dtype=np.float32,
    ).reshape(-1)
    if cleaned_f.size != near_f.size:
        if cleaned_f.size > near_f.size:
            cleaned_f = cleaned_f[: near_f.size]
        else:
            cleaned_f = np.pad(cleaned_f, (0, near_f.size - cleaned_f.size))

    cleaned_i16 = np.clip(cleaned_f * 32768.0, -32768.0, 32767.0).astype(np.int16)
    out_db = rms_dbfs(cleaned_i16)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return EchoCancellationResult(
        audio=cleaned_i16,
        applied=True,
        reason="applied",
        delay_ms=delay_ms,
        correlation_z=confidence_z,
        input_rms_db=near_db,
        output_rms_db=out_db,
        far_rms_db=far_db,
        elapsed_ms=elapsed_ms,
    )


def _active_time(t_mono: float, pause_spans: Sequence[tuple[float, float]]) -> float | None:
    """Map wall ``t_mono`` onto active recording time; None when inside a pause."""
    paused_before = 0.0
    for pause_start, pause_end in pause_spans:
        if pause_end < pause_start:
            continue
        if pause_start <= t_mono < pause_end:
            return None
        if t_mono >= pause_end:
            paused_before += pause_end - pause_start
    return t_mono - paused_before


def build_aligned_reference_16k(
    blocks: Sequence[ReferenceBlock],
    target_samples: int,
    *,
    mic_start_mono: float | None,
    pause_spans: Sequence[tuple[float, float]] = (),
    target_rate: int = AEC_SAMPLE_RATE,
) -> np.ndarray:
    """Assemble timestamped loopback blocks into a 16 kHz array on the mic timeline."""
    out = np.zeros(max(0, int(target_samples)), dtype=np.int16)
    if not blocks or out.size == 0:
        return out

    # Filter out paused blocks and collapse pause gaps on the timeline.
    active_blocks: list[tuple[float, int, np.ndarray]] = []
    for block in blocks:
        if block.frames.size == 0 or block.sample_rate <= 0:
            continue
        mid = block.t_mono + (block.frames.size / float(block.sample_rate)) * 0.5
        mapped_mid = _active_time(mid, pause_spans)
        if mapped_mid is None:
            continue
        mapped_start = mapped_mid - (block.frames.size / float(block.sample_rate)) * 0.5
        active_blocks.append((mapped_start, int(block.sample_rate), block.frames))
    if not active_blocks:
        return out

    origin_mono = (
        float(mic_start_mono)
        if mic_start_mono is not None
        else active_blocks[0][0]
    )

    # Group contiguous same-rate runs so PyAV resamples each run smoothly without
    # per-block discontinuities.
    runs: list[tuple[float, int, list[np.ndarray]]] = []
    run_start = active_blocks[0][0]
    run_rate = active_blocks[0][1]
    run_frames: list[np.ndarray] = [active_blocks[0][2]]
    expected_next = run_start + active_blocks[0][2].size / float(run_rate)

    for block_start, rate, frames in active_blocks[1:]:
        gap = block_start - expected_next
        if rate != run_rate or abs(gap) > GAP_TOLERANCE_S:
            runs.append((run_start, run_rate, run_frames))
            run_start = block_start
            run_rate = rate
            run_frames = [frames]
            expected_next = block_start + frames.size / float(rate)
        else:
            run_frames.append(frames)
            expected_next += frames.size / float(rate)
    runs.append((run_start, run_rate, run_frames))

    for start_t, rate, chunks in runs:
        native = np.concatenate(chunks)
        resampled = resample_int16_mono(native, rate, target_rate)
        if resampled.size == 0:
            continue
        offset = int(round((start_t - origin_mono) * float(target_rate)))
        if offset >= out.size:
            continue
        if offset < 0:
            src_start = -offset
            if src_start >= resampled.size:
                continue
            copy_n = min(resampled.size - src_start, out.size)
            out[:copy_n] = resampled[src_start : src_start + copy_n]
        else:
            copy_n = min(resampled.size, out.size - offset)
            out[offset : offset + copy_n] = resampled[:copy_n]

    return out


def cancel_echo_in_wav(
    wav_path: str,
    reference: CapturedReference,
    *,
    mic_start_mono: float | None = None,
    pause_spans: Sequence[tuple[float, float]] = (),
    end_padding_ms: int | None = None,
    canceller_factory: Callable[..., Any] | None = None,
) -> EchoCancellationResult:
    """Apply offline WebRTC AEC3 to ``wav_path`` in place (atomically)."""
    if reference.overflowed:
        return EchoCancellationResult(None, False, "reference_overflow")
    if not reference.blocks:
        return EchoCancellationResult(None, False, "no_reference_blocks")

    with wave.open(wav_path, "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)

    if channels != 1 or sampwidth != 2 or nframes <= 0:
        return EchoCancellationResult(None, False, "unsupported_wav_format")

    mic_native = np.frombuffer(raw, dtype=np.int16)
    pad_ms = config.END_PADDING_MS if end_padding_ms is None else int(end_padding_ms)
    pad_native = max(0, int(round(rate * (pad_ms / 1000.0)))) if pad_ms > 0 else 0
    if pad_native > 0 and mic_native.size > pad_native:
        mic_body = mic_native[:-pad_native]
    else:
        mic_body = mic_native

    mic_16k = resample_int16_mono(mic_body, rate, AEC_SAMPLE_RATE)
    far_16k = build_aligned_reference_16k(
        reference.blocks,
        mic_16k.size,
        mic_start_mono=mic_start_mono,
        pause_spans=pause_spans,
        target_rate=AEC_SAMPLE_RATE,
    )

    result = cancel_echo(
        mic_16k,
        far_16k,
        sample_rate=AEC_SAMPLE_RATE,
        canceller_factory=canceller_factory,
    )
    if not result.applied or result.audio is None:
        return result

    pad_16k = (
        np.zeros(max(0, int(round(AEC_SAMPLE_RATE * (pad_ms / 1000.0)))), dtype=np.int16)
        if pad_ms > 0
        else np.zeros(0, dtype=np.int16)
    )
    final_pcm = np.concatenate((result.audio, pad_16k)) if pad_16k.size else result.audio

    directory = os.path.dirname(os.path.abspath(wav_path)) or os.curdir
    temp_fd, temp_path = tempfile.mkstemp(suffix=".wav", dir=directory)
    try:
        with os.fdopen(temp_fd, "wb") as temp_file:
            temp_fd = -1
            with wave.open(temp_file, "wb") as out_wf:
                out_wf.setnchannels(1)
                out_wf.setsampwidth(2)
                out_wf.setframerate(AEC_SAMPLE_RATE)
                out_wf.writeframes(final_pcm.tobytes())
            try:
                append_origination(temp_file, datetime.now())
            except Exception:
                logger.debug("Could not stamp WAV origination after AEC", exc_info=True)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, wav_path)
        temp_path = ""
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    return result


class LoopbackReferenceCapture:
    """Best-effort system-audio reference recorder paired with one dictation."""

    def __init__(self, *, max_bytes: int = MAX_REFERENCE_BYTES) -> None:
        self._max_bytes = max(1, int(max_bytes))
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._blocks: list[ReferenceBlock] = []
        self._bytes = 0
        self._overflowed = False
        self._source: Any = None
        self._backend = ""
        self._starter: threading.Thread | None = None

    def start(self) -> None:
        """Open system-audio loopback asynchronously; never blocks recording start."""
        if self._starter is not None:
            return
        self._starter = threading.Thread(
            target=self._open_loopback,
            name="dictation-aec-loopback-start",
            daemon=True,
        )
        self._starter.start()

    def _open_loopback(self) -> None:
        if self._stopped.is_set():
            return
        source = None
        backend = ""
        try:
            source, backend = self._start_best_source()
        except Exception:
            logger.warning(
                "Beta echo cancellation: could not open system-audio loopback",
                exc_info=True,
            )
            return

        if source is None:
            logger.info(
                "Beta echo cancellation: system-audio loopback unavailable; "
                "keeping raw microphone audio"
            )
            return

        # If stop() landed while the source was opening, tear it down at once.
        stop_now = False
        with self._lock:
            if self._stopped.is_set():
                stop_now = True
            else:
                self._source = source
                self._backend = backend
        if stop_now:
            self._safe_stop_source(source)
        else:
            logger.info("Beta echo cancellation: loopback reference started (%s)", backend)

    def _start_best_source(self) -> tuple[Any, str]:
        """Try WASAPI loopback first on Windows, then ScreenCaptureKit / SoundCard."""
        from meeting.capture.devices import find_loopback_device
        from meeting.capture.sck_stream import ScreenCaptureKitLoopbackSource
        from meeting.capture.sd_stream import SdCaptureSource
        from meeting.capture.soundcard_stream import SoundcardLoopbackSource
        from meeting.interfaces import CHANNEL_LOOPBACK

        if self._stopped.is_set():
            return None, ""

        loop_dev = None
        try:
            loop_dev = find_loopback_device()
        except Exception:
            logger.debug("Loopback device probe failed for dictation AEC", exc_info=True)

        if loop_dev is not None and not self._stopped.is_set():
            source = SdCaptureSource(
                CHANNEL_LOOPBACK,
                loop_dev["index"],
                loop_dev["samplerate"],
                loop_dev["channels"],
            )
            try:
                with audio_devices.portaudio_lock:
                    source.start(self._on_block)
                if source.is_active():
                    return source, "wasapi"
                self._safe_stop_source(source)
            except Exception:
                logger.debug("WASAPI loopback start failed for dictation AEC", exc_info=True)
                self._safe_stop_source(source)

        for cls, name in (
            (ScreenCaptureKitLoopbackSource, "screencapturekit"),
            (SoundcardLoopbackSource, "soundcard"),
        ):
            if self._stopped.is_set():
                return None, ""
            try:
                if not cls.available():
                    continue
                source = cls()
                source.start(self._on_block)
                if source.is_active():
                    return source, name
                self._safe_stop_source(source)
            except Exception:
                logger.debug("%s loopback start failed for dictation AEC", name, exc_info=True)

        return None, ""

    def _on_block(self, block: Any) -> None:
        if self._stopped.is_set():
            return
        frames = getattr(block, "frames", None)
        if frames is None or len(frames) == 0:
            return
        arr = np.asarray(frames, dtype=np.int16).reshape(-1)
        nbytes = int(arr.nbytes)
        with self._lock:
            if self._stopped.is_set() or self._overflowed:
                return
            if self._bytes + nbytes > self._max_bytes:
                self._overflowed = True
                self._blocks.clear()
                logger.warning(
                    "Beta echo cancellation: reference buffer limit reached; "
                    "skipping echo cancellation for this recording"
                )
                return
            self._blocks.append(
                ReferenceBlock(
                    t_mono=float(block.t_mono),
                    sample_rate=int(block.sample_rate),
                    frames=arr.copy(),
                )
            )
            self._bytes += nbytes

    def stop(self, *, discard: bool = False, timeout: float = STOP_JOIN_TIMEOUT_S) -> CapturedReference | None:
        """Stop loopback capture and return collected blocks (or None when discarded)."""
        self._stopped.set()
        with self._lock:
            source, self._source = self._source, None
        if source is not None:
            self._safe_stop_source(source)
        starter = self._starter
        self._starter = None
        if starter is not None and starter.is_alive() and starter is not threading.current_thread():
            starter.join(timeout=timeout)
            with self._lock:
                late_source, self._source = self._source, None
            if late_source is not None:
                self._safe_stop_source(late_source)
        with self._lock:
            blocks = tuple(self._blocks)
            backend = self._backend
            overflowed = self._overflowed
            self._blocks.clear()
            self._bytes = 0
        if discard:
            return None
        return CapturedReference(blocks=blocks, backend=backend, overflowed=overflowed)

    @staticmethod
    def _safe_stop_source(source: Any) -> None:
        try:
            source.stop()
        except Exception:
            logger.debug("Error stopping dictation AEC loopback source", exc_info=True)
