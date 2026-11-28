"""Bounded decoding windows for file transcription; timestamps stay in source time.

Uses PyAV (FFmpeg) when installed, and falls back to a pure-Python/NumPy WAV
decoder and stateful windowed-sinc polyphase resampler when ``av`` is absent
(such as in Client-Only builds). Non-WAV compressed files without PyAV delegate
to the paired host's ``file_decode_*`` RPC when available.
"""
from __future__ import annotations

import math
import os
import struct
import wave
from collections.abc import Iterator
from fractions import Fraction

import numpy as np

from services.wav_metadata import is_mono_pcm16

SAMPLE_RATE = 16000
MAX_SAMPLES = 30 * SAMPLE_RATE
MIN_SAMPLES = 24 * SAMPLE_RATE
# Before it decodes 16-bit PCM from a WAV, FFmpeg searches the first 32
# packets for a compressed stream carried inside it (DTS or AC-3, as on a
# surround CD), which takes two channels. A one-channel recording has
# nothing to find, and the search cost every dictation of five seconds or
# more 17 ms at its stop (November 2026). This ends it after one packet;
# the frames decoded are the same.
_MONO_PCM_OPTIONS = {"probesize": "4096"}
#: Frames read per chunk when streaming WAV files without PyAV.
_WAV_CHUNK_FRAMES = 65536
#: Output samples computed per vectorized block inside ``NumpyAudioResampler``.
_RESAMPLE_BLOCK = 4096
#: Half-width lobes for windowed-sinc anti-aliasing filter.
_SINC_LOBES = 4
#: Cap on half-width tap radius in input samples.
_MAX_HALF_WIDTH = 32


def has_pyav() -> bool:
    """True when PyAV (``av``) can be imported in this environment."""
    try:
        import av  # noqa: F401
    except ImportError:
        return False
    return True


def is_wav_file(audio_path: str) -> bool:
    """True when ``audio_path`` begins with a RIFF/WAVE header."""
    try:
        with open(audio_path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return False
    return len(head) == 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"


class PcmFrame:
    """Minimal frame matching PyAV's ``AudioFrame`` surface for ``WindowSplitter``."""

    def __init__(self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
        self._samples = np.ascontiguousarray(
            np.asarray(samples, dtype=np.int16).reshape(-1)
        )
        self.sample_rate = int(sample_rate)
        self.pts = None

    def to_ndarray(self) -> np.ndarray:
        return self._samples.reshape(1, -1)


class NumpyAudioResampler:
    """Stateful streaming 16-bit mono resampler using Hann-windowed sinc interpolation.

    Each output sample ``k`` is computed solely from its global input position
    ``k * src_rate / dst_rate`` and its fixed surrounding window of input
    samples, so chunking the input stream arbitrarily produces bit-identical
    output to decoding the whole file at once.
    """

    def __init__(self, rate: int = SAMPLE_RATE) -> None:
        self.rate = int(rate)
        self._src_rate: int | None = None
        self._buf = np.empty(0, dtype=np.float64)
        self._buf_start = 0
        self._in_total = 0
        self._out_total = 0
        self._flushed = False

    def _half_width(self, src_rate: int) -> int:
        if src_rate == self.rate:
            return 0
        cutoff = min(1.0, self.rate / float(src_rate))
        return min(_MAX_HALF_WIDTH, max(_SINC_LOBES, int(math.ceil(_SINC_LOBES / cutoff))))

    def _compute_range(
        self, start_k: int, stop_k: int, src_rate: int, *, clamp_end: bool
    ) -> np.ndarray:
        count = stop_k - start_k
        if count <= 0 or self._in_total <= 0:
            return np.empty(0, dtype=np.int16)
        cutoff = min(1.0, self.rate / float(src_rate))
        half = self._half_width(src_rate)
        offs = np.arange(-half + 1, half + 1, dtype=np.int64)
        step = src_rate / float(self.rate)
        out = np.empty(count, dtype=np.int16)
        buf = self._buf
        buf_start = self._buf_start
        max_idx = self._in_total - 1

        for pos_out in range(0, count, _RESAMPLE_BLOCK):
            end_out = min(pos_out + _RESAMPLE_BLOCK, count)
            ks = np.arange(start_k + pos_out, start_k + end_out, dtype=np.float64)
            positions = ks * step
            centers = np.floor(positions).astype(np.int64)
            indices = centers[:, None] + offs[None, :]
            delta = positions[:, None] - indices
            # Hann-windowed sinc with anti-aliasing cutoff; exact unity DC gain.
            weights = np.sinc(delta * cutoff) * (
                0.5 + 0.5 * np.cos(math.pi * delta / float(half))
            )
            weight_sum = np.sum(weights, axis=1, keepdims=True)
            weight_sum = np.where(weight_sum == 0.0, 1.0, weight_sum)
            weights /= weight_sum

            clamped = np.clip(indices, 0, max_idx if clamp_end else max_idx)
            local = clamped - buf_start
            vals = np.sum(buf[local] * weights, axis=1)
            out[pos_out:end_out] = np.clip(np.rint(vals), -32768.0, 32767.0).astype(
                np.int16
            )
        return out

    def resample(self, frame) -> list[PcmFrame]:
        """Accept a ``PcmFrame`` (or PyAV-like frame) or ``None`` to flush."""
        if frame is None:
            if self._flushed or self._src_rate is None or self._in_total <= 0:
                self._flushed = True
                return []
            self._flushed = True
            if self._src_rate == self.rate:
                return []
            total_out = int(round(self._in_total * self.rate / float(self._src_rate)))
            if total_out <= self._out_total:
                return []
            samples = self._compute_range(
                self._out_total, total_out, self._src_rate, clamp_end=True
            )
            self._out_total = total_out
            return [PcmFrame(samples, self.rate)] if samples.size else []

        src_rate = int(getattr(frame, "sample_rate", self.rate) or self.rate)
        raw = frame.to_ndarray().reshape(-1)
        if raw.size == 0:
            return []
        if self._src_rate is None:
            self._src_rate = src_rate
        elif self._src_rate != src_rate:
            raise ValueError(
                f"Resampler source rate changed mid-stream ({self._src_rate} -> {src_rate})"
            )

        if src_rate == self.rate:
            arr = np.ascontiguousarray(raw, dtype=np.int16)
            self._in_total += arr.size
            self._out_total += arr.size
            return [PcmFrame(arr, self.rate)]

        chunk_f64 = raw.astype(np.float64, copy=False)
        self._buf = (
            np.concatenate((self._buf, chunk_f64)) if self._buf.size else chunk_f64.copy()
        )
        self._in_total += chunk_f64.size

        half = self._half_width(src_rate)
        # Emit all k whose rightmost tap floor(k * src / dst) + half < _in_total.
        # floor(k * src / dst) <= _in_total - 1 - half  <==>  k * src / dst < _in_total - half.
        limit_pos = self._in_total - half
        if limit_pos <= 0:
            return []
        # Conservative upper bound, adjusted down if float rounding nudged over.
        stop_k = int(math.ceil(limit_pos * self.rate / float(src_rate)))
        step = src_rate / float(self.rate)
        while stop_k > self._out_total and int(math.floor((stop_k - 1) * step)) + half >= self._in_total:
            stop_k -= 1
        while int(math.floor(stop_k * step)) + half < self._in_total:
            stop_k += 1

        if stop_k <= self._out_total:
            return []
        samples = self._compute_range(self._out_total, stop_k, src_rate, clamp_end=False)
        self._out_total = stop_k

        # Trim samples no future k can ever touch (preserving index 0 while clamping needs it).
        next_min = int(math.floor(self._out_total * step)) - half + 1
        keep_from = max(0, next_min)
        if keep_from > self._buf_start:
            drop = keep_from - self._buf_start
            self._buf = self._buf[drop:]
            self._buf_start = keep_from
        return [PcmFrame(samples, self.rate)] if samples.size else []


def resample_int16_mono_numpy(
    samples: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE
) -> np.ndarray:
    """Pure-NumPy int16 mono resampling using ``NumpyAudioResampler``."""
    arr = np.ascontiguousarray(np.asarray(samples, dtype=np.int16).reshape(-1))
    if arr.size == 0 or int(src_rate) == int(dst_rate):
        return arr.copy()
    conv = NumpyAudioResampler(rate=int(dst_rate))
    pieces: list[np.ndarray] = []
    for frame in conv.resample(PcmFrame(arr, int(src_rate))):
        chunk = frame.to_ndarray().reshape(-1)
        if chunk.size:
            pieces.append(chunk)
    for frame in conv.resample(None):
        chunk = frame.to_ndarray().reshape(-1)
        if chunk.size:
            pieces.append(chunk)
    if not pieces:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(pieces).astype(np.int16, copy=False)


def _pcm_bytes_to_mono_int16(
    raw: bytes, sampwidth: int, channels: int, *, ieee_float: bool = False
) -> np.ndarray:
    """Decode raw little-endian WAV bytes into 1-D mono int16 samples."""
    if not raw or channels <= 0:
        return np.empty(0, dtype=np.int16)
    frame_bytes = sampwidth * channels
    usable = (len(raw) // frame_bytes) * frame_bytes
    if usable <= 0:
        return np.empty(0, dtype=np.int16)
    view = memoryview(raw)[:usable]

    if ieee_float:
        if sampwidth == 4:
            flt = np.frombuffer(view, dtype="<f4").astype(np.float64, copy=False)
        elif sampwidth == 8:
            flt = np.frombuffer(view, dtype="<f8")
        else:
            raise ValueError(f"Unsupported IEEE float WAV sample width: {sampwidth}")
        if channels > 1:
            flt = flt.reshape(-1, channels).mean(axis=1)
        return np.clip(np.rint(flt * 32768.0), -32768.0, 32767.0).astype(np.int16)

    if sampwidth == 1:
        # 8-bit PCM WAV is unsigned [0, 255] centered at 128.
        arr_i32 = (np.frombuffer(view, dtype=np.uint8).astype(np.int32) - 128) << 8
        if channels > 1:
            arr_i32 = np.rint(arr_i32.reshape(-1, channels).mean(axis=1)).astype(np.int32)
        return np.clip(arr_i32, -32768, 32767).astype(np.int16)

    if sampwidth == 2:
        arr_i16 = np.frombuffer(view, dtype="<i2")
        if channels == 1:
            return arr_i16.copy()
        mean = arr_i16.reshape(-1, channels).astype(np.int32).mean(axis=1)
        return np.clip(np.rint(mean), -32768.0, 32767.0).astype(np.int16)

    if sampwidth == 3:
        triplets = np.frombuffer(view, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        val = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
        val = np.where(val >= 0x800000, val - 0x1000000, val)
        if channels > 1:
            val = np.rint(val.reshape(-1, channels).mean(axis=1)).astype(np.int32)
        return np.clip(val >> 8, -32768, 32767).astype(np.int16)

    if sampwidth == 4:
        arr_i32 = np.frombuffer(view, dtype="<i4") >> 16
        if channels > 1:
            arr_i32 = np.rint(arr_i32.reshape(-1, channels).mean(axis=1)).astype(np.int32)
        return np.clip(arr_i32, -32768, 32767).astype(np.int16)

    raise ValueError(f"Unsupported WAV sample width: {sampwidth} bytes")


def _parse_riff_wav_info(audio_path: str) -> tuple[int, int, int, int, int, bool]:
    """Parse RIFF/WAVE ``fmt `` and ``data`` chunks (handles PCM and IEEE float).

    Returns ``(sample_rate, channels, sampwidth, data_offset, data_size, ieee_float)``.
    """
    with open(audio_path, "rb") as handle:
        head = handle.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("Not a RIFF/WAVE file")
        fmt_info = None
        data_span = None
        while True:
            chunk_hdr = handle.read(8)
            if len(chunk_hdr) < 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_hdr)
            chunk_start = handle.tell()
            if chunk_id == b"fmt " and chunk_size >= 16:
                fmt_raw = handle.read(min(chunk_size, 40))
                tag, channels, rate, _byte_rate, _align, bits = struct.unpack_from(
                    "<HHIIHH", fmt_raw, 0
                )
                ieee_float = tag == 3
                if tag == 0xFFFE and len(fmt_raw) >= 26:
                    sub_tag = struct.unpack_from("<H", fmt_raw, 24)[0]
                    tag = sub_tag
                    ieee_float = sub_tag == 3
                if tag not in (1, 3):
                    raise ValueError(f"Unsupported WAV format tag: {tag}")
                if rate <= 0 or channels <= 0 or bits not in (8, 16, 24, 32, 64):
                    raise ValueError("Invalid WAV format parameters")
                fmt_info = (rate, channels, bits // 8, ieee_float)
            elif chunk_id == b"data":
                data_span = (chunk_start, chunk_size)
                if fmt_info is not None:
                    break
            handle.seek(chunk_start + chunk_size + (chunk_size & 1), os.SEEK_SET)
        if fmt_info is None or data_span is None:
            raise ValueError("Incomplete WAV file")
        rate, channels, sampwidth, ieee_float = fmt_info
        return rate, channels, sampwidth, data_span[0], data_span[1], ieee_float


def probe_wav_header(audio_path: str) -> tuple[float, int, int]:
    """Return ``(duration_seconds, sample_rate, channels)`` for a WAV without PyAV."""
    try:
        with wave.open(audio_path, "rb") as wf:
            sample_rate = wf.getframerate()
            channels = wf.getnchannels()
            nframes = wf.getnframes()
        if sample_rate <= 0 or channels <= 0:
            raise ValueError("Invalid WAV header")
        return nframes / float(sample_rate), sample_rate, channels
    except wave.Error:
        rate, channels, sampwidth, _offset, data_size, _is_flt = _parse_riff_wav_info(
            audio_path
        )
        nframes = data_size // (sampwidth * channels)
        return nframes / float(rate), rate, channels


def iter_wav_mono_int16(
    audio_path: str, chunk_frames: int = _WAV_CHUNK_FRAMES
) -> Iterator[tuple[np.ndarray, int, int]]:
    """Stream native-rate mono int16 PCM blocks from a WAV file without PyAV."""
    try:
        wf = wave.open(audio_path, "rb")
    except wave.Error:
        rate, channels, sampwidth, offset, data_size, ieee_float = _parse_riff_wav_info(
            audio_path
        )
        frame_bytes = sampwidth * channels
        remaining = (data_size // frame_bytes) * frame_bytes
        read_bytes = max(frame_bytes, chunk_frames * frame_bytes)
        with open(audio_path, "rb") as handle:
            handle.seek(offset, os.SEEK_SET)
            while remaining > 0:
                raw = handle.read(min(read_bytes, remaining))
                if not raw:
                    break
                remaining -= len(raw)
                samples = _pcm_bytes_to_mono_int16(
                    raw, sampwidth, channels, ieee_float=ieee_float
                )
                if samples.size:
                    yield samples, rate, channels
        return

    with wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        if rate <= 0 or channels <= 0:
            raise ValueError("Invalid WAV sample rate or channel count")
        while True:
            raw = wf.readframes(chunk_frames)
            if not raw:
                break
            samples = _pcm_bytes_to_mono_int16(raw, sampwidth, channels)
            if samples.size:
                yield samples, rate, channels


def split_point(audio: np.ndarray) -> int:
    if len(audio) < MAX_SAMPLES:
        return len(audio)
    region = audio[MIN_SAMPLES:MAX_SAMPLES]
    energy = np.mean(region.reshape(-1, 1600) ** 2, axis=1)
    return MIN_SAMPLES + (int(np.argmin(energy)) + 1) * 1600


def resampler():
    """The 16 kHz mono converter behind every window.

    Incremental dictation runs recorder PCM through its own instance and
    relies on matching ``windows()`` bit for bit, so both are built here.
    Falls back to ``NumpyAudioResampler`` when PyAV is not installed.
    """
    try:
        import av
    except ImportError:
        return NumpyAudioResampler(rate=SAMPLE_RATE)

    return av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)


def make_pcm_frame(samples: np.ndarray, sample_rate: int):
    """Wrap mono int16 ``samples`` in an ``av.AudioFrame`` or ``PcmFrame``."""
    try:
        import av
    except ImportError:
        return PcmFrame(samples, sample_rate)

    frame = av.AudioFrame.from_ndarray(
        samples.reshape(1, -1), format="s16", layout="mono"
    )
    frame.sample_rate = int(sample_rate)
    frame.time_base = Fraction(1, int(sample_rate))
    frame.pts = None
    return frame


class WindowSplitter:
    """``windows()``'s chunking for a 16 kHz stream that arrives in pieces.

    A split looks only at the first MAX_SAMPLES pending, so the windows are a
    function of the sample stream alone and do not depend on how it was cut
    into frames. Pieces are joined once per window: concatenating on every
    resampler frame spent 133 ms copying buffers for an 88.8 s dictation
    (September 2026), more than PyAV took to decode and resample it.
    """

    def __init__(self):
        self._pieces: list[np.ndarray] = []
        self._pending = 0
        #: Samples already returned in earlier windows.
        self.offset = 0

    @property
    def total(self) -> int:
        """Samples pushed so far."""
        return self.offset + self._pending

    def push(self, frames) -> list[tuple[float, np.ndarray]]:
        """Add resampler output frames; return the windows they complete."""
        ready = []
        for frame in frames:
            samples = frame.to_ndarray().reshape(-1).astype(np.float32) / 32768.
            self._pieces.append(samples)
            self._pending += len(samples)
            if self._pending < MAX_SAMPLES:
                continue
            pending = np.concatenate(self._pieces)
            while len(pending) >= MAX_SAMPLES:
                end = split_point(pending)
                ready.append((self.offset / SAMPLE_RATE, pending[:end]))
                self.offset += end
                pending = pending[end:]
            self._pieces, self._pending = [pending], len(pending)
        return ready

    def finish(self) -> list[tuple[float, np.ndarray]]:
        """Return the final partial window, if any audio is left."""
        if not self._pending:
            return []
        pending = np.concatenate(self._pieces)
        window = (self.offset / SAMPLE_RATE, pending)
        self.offset += len(pending)
        self._pieces, self._pending = [], 0
        return [window]


def _open(audio_path: str):
    import av

    if is_mono_pcm16(audio_path):
        return av.open(audio_path, options=_MONO_PCM_OPTIONS)
    return av.open(audio_path)


def _wav_windows(audio_path: str) -> Iterator[tuple[float, np.ndarray]]:
    """Pure-Python/NumPy ``windows()`` implementation for WAV files."""
    splitter = WindowSplitter()
    convert = NumpyAudioResampler(rate=SAMPLE_RATE)
    for samples, rate, _channels in iter_wav_mono_int16(audio_path):
        yield from splitter.push(convert.resample(PcmFrame(samples, rate)))
    yield from splitter.push(convert.resample(None))
    yield from splitter.finish()


def _remote_file_windows(audio_path: str) -> Iterator[tuple[float, np.ndarray]]:
    """Delegate compressed audio decoding to the paired host when PyAV is absent."""
    try:
        from services.remote_asr.meeting_client import decode_audio_windows_remote
    except ImportError as exc:
        raise RuntimeError(
            "Decoding compressed audio files requires PyAV/FFmpeg locally or a "
            "paired OpenWhisper host."
        ) from exc
    yield from decode_audio_windows_remote(audio_path)


def windows(audio_path: str):
    try:
        import av  # noqa: F401
    except ImportError:
        if is_wav_file(audio_path):
            yield from _wav_windows(audio_path)
            return
        yield from _remote_file_windows(audio_path)
        return

    splitter = WindowSplitter()
    convert = resampler()
    with _open(audio_path) as container:
        for frame in container.decode(audio=0):
            frame.pts = None
            yield from splitter.push(convert.resample(frame))
        yield from splitter.push(convert.resample(None))
        yield from splitter.finish()
