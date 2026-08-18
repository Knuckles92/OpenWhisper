"""Audio helpers for the post-meeting OpenAI speaker-identification pass.

Pure functions: MP3 encode via PyAV, window a session against the API's
25 MB file cap, and cut 2–10 s reference clips as data URLs. No network.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

#: Bitrate used for loopback uploads and reference clips.
DEFAULT_BITRATE = 32000

#: Conservative payload budget so a single request stays under the API's
#: 25 MB file limit after container headers.
DEFAULT_BYTE_BUDGET = 20 * 1024 * 1024

#: Overlap between successive windows when a meeting does not fit in one
#: request. Later windows reuse clips from earlier speakers.
DEFAULT_WINDOW_OVERLAP_S = 2.0

#: OpenAI known-speaker clips must be between 2 and 10 seconds.
CLIP_MIN_S = 2.0
CLIP_MAX_S = 10.0


def estimate_mp3_bytes(duration_s: float, bitrate: int = DEFAULT_BITRATE) -> int:
    """Upper-bound encoded size for ``duration_s`` of audio at ``bitrate``.

    Adds a small header slack so window planning does not graze the cap.
    """
    seconds = max(0.0, float(duration_s))
    rate = max(1, int(bitrate))
    return int(seconds * rate / 8.0 * 1.05) + 1024


def plan_windows(
    sample_count: int,
    sample_rate: int,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
    bitrate: int = DEFAULT_BITRATE,
    overlap_s: float = DEFAULT_WINDOW_OVERLAP_S,
) -> List[Tuple[int, int]]:
    """Return ``[start, end)`` sample ranges that each fit ``byte_budget``.

    One window covering the whole session when the estimated MP3 fits;
    otherwise overlapping windows sized from the budget.

    Args:
        sample_count: Total int16 samples.
        sample_rate: Sample rate of those samples.
        byte_budget: Maximum encoded size per window, in bytes.
        bitrate: Target MP3 bitrate in bits/s.
        overlap_s: Samples of the previous window reused at the next start.

    Returns:
        Inclusive-start exclusive-end ranges covering ``[0, sample_count)``.
    """
    total = max(0, int(sample_count))
    rate = max(1, int(sample_rate))
    if total <= 0:
        return []
    duration_s = total / float(rate)
    if estimate_mp3_bytes(duration_s, bitrate) <= int(byte_budget):
        return [(0, total)]

    usable = max(1.0, float(byte_budget) - 1024.0) / 1.05
    max_s = (usable * 8.0) / float(max(1, int(bitrate)))
    max_s = max(10.0, max_s)
    max_n = max(1, int(round(max_s * rate)))
    overlap_n = max(0, int(round(float(overlap_s) * rate)))
    if overlap_n >= max_n:
        overlap_n = max_n // 8

    ranges: List[Tuple[int, int]] = []
    position = 0
    while position < total:
        end = min(total, position + max_n)
        ranges.append((position, end))
        if end >= total:
            break
        nxt = end - overlap_n
        if nxt <= position:
            nxt = end
        position = nxt
    return ranges


def clip_sample_range(
    sample_count: int,
    sample_rate: int,
    start_s: float,
    end_s: float,
    *,
    origin_s: float = 0.0,
) -> Tuple[int, int]:
    """Clamp a meeting-clock interval to a 2–10 s sample range.

    Short intervals expand around the original span (then to either edge of
    the file). Long intervals keep the first 10 seconds.
    """
    total = max(0, int(sample_count))
    rate = max(1, int(sample_rate))
    rel_start = float(start_s) - float(origin_s)
    rel_end = float(end_s) - float(origin_s)
    start = int(round(rel_start * rate))
    end = int(round(rel_end * rate))
    start = max(0, min(start, total))
    end = max(0, min(end, total))
    if end < start:
        start, end = end, start
    duration = (end - start) / float(rate) if rate else 0.0
    min_n = int(round(CLIP_MIN_S * rate))
    max_n = int(round(CLIP_MAX_S * rate))
    if duration < CLIP_MIN_S:
        need = min_n - (end - start)
        extra_left = need // 2
        extra_right = need - extra_left
        start = max(0, start - extra_left)
        end = min(total, end + extra_right)
        still = min_n - (end - start)
        if still > 0:
            start = max(0, start - still)
        still = min_n - (end - start)
        if still > 0:
            end = min(total, end + still)
    elif end - start > max_n:
        end = min(total, start + max_n)
    return start, end


def encode_mp3(
    frames: np.ndarray,
    sample_rate: int,
    bitrate: int = DEFAULT_BITRATE,
) -> bytes:
    """Encode mono int16 PCM as an MP3 byte string.

    Args:
        frames: Mono int16 samples.
        sample_rate: Sample rate of ``frames``.
        bitrate: Target bitrate in bits/s.

    Returns:
        Complete MP3 file bytes.

    Raises:
        ValueError: When ``frames`` is empty.
        ImportError: When PyAV is unavailable.
    """
    samples = np.asarray(frames, dtype=np.int16).reshape(-1)
    if samples.size == 0:
        raise ValueError("empty audio")
    rate = max(1, int(sample_rate))
    try:
        import av
    except ImportError:
        logger.exception("PyAV is required to encode meeting audio for upload")
        raise

    buf = io.BytesIO()
    container = av.open(buf, mode="w", format="mp3")
    try:
        stream = container.add_stream("libmp3lame", rate=rate)
        stream.bit_rate = int(bitrate)
        try:
            stream.layout = "mono"
        except Exception:
            pass
        layout = getattr(stream, "layout", None)
        channels = 1
        if layout is not None:
            channels = int(getattr(layout, "nb_channels", 1) or 1)
        if channels <= 1:
            ndarray = samples.reshape(1, -1)
            frame_layout = "mono"
        else:
            ndarray = np.repeat(samples.reshape(1, -1), channels, axis=0)
            frame_layout = str(layout)
        audio = av.AudioFrame.from_ndarray(
            ndarray, format="s16", layout=frame_layout,
        )
        audio.sample_rate = rate
        for packet in stream.encode(audio):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()
    return buf.getvalue()


def cut_reference_clip(
    frames: np.ndarray,
    sample_rate: int,
    start_s: float,
    end_s: float,
    *,
    origin_s: float = 0.0,
    bitrate: int = DEFAULT_BITRATE,
) -> Optional[str]:
    """Return a ``data:audio/mpeg;base64,...`` clip, or None when empty."""
    samples = np.asarray(frames, dtype=np.int16).reshape(-1)
    start, end = clip_sample_range(
        samples.size, sample_rate, start_s, end_s, origin_s=origin_s,
    )
    if end <= start:
        return None
    try:
        mp3 = encode_mp3(samples[start:end], sample_rate, bitrate)
    except Exception:
        logger.exception("Failed to encode a speaker reference clip")
        return None
    if not mp3:
        return None
    encoded = base64.b64encode(mp3).decode("ascii")
    return f"data:audio/mpeg;base64,{encoded}"
