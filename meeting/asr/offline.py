"""Post-meeting clean ASR: silence-split a session WAV and re-decode it.

Live capture still uses short 5/20-second chunks for dashboard latency. After
End, this module re-cuts the continuous per-channel recording on longer quiet
gaps and transcribes sequentially with cross-window conditioning.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from meeting.asr.audio import WHISPER_SAMPLE_RATE, load_wav_int16, prepare_for_whisper
from meeting.capture.spool import (
    QUIET_RMS,
    find_cut_point,
    load_session_meta,
    resolve_session_wav,
    session_meta_path,
)
from meeting.interfaces import CHANNELS, TranscriptSegment

logger = logging.getLogger(__name__)

#: Preferred offline chunk duration before quiet-cut scanning starts.
OFFLINE_TARGET_SEC = 15.0

#: Hard maximum when speech never pauses. Stays close to the live ceiling
#: so Whisper VAD/decode behave like the proven draft path.
OFFLINE_MAX_SEC = 25.0

#: Quiet stretch required to cut (longer than the live 400 ms window).
OFFLINE_QUIET_WINDOW_S = 0.7

#: Overlap carried into the next window so Whisper has right-context.
#: Zero until spanning-segment drop is proven not to delete real speech;
#: live draft chunks are also contiguous (no overlap) at 25% tcWER.
OFFLINE_OVERLAP_S = 0.0


def offline_cut_ranges(
    sample_count: int,
    sample_rate: int,
    audio: Optional[np.ndarray] = None,
    *,
    target_sec: float = OFFLINE_TARGET_SEC,
    max_sec: float = OFFLINE_MAX_SEC,
    quiet_window_s: float = OFFLINE_QUIET_WINDOW_S,
    overlap_s: float = OFFLINE_OVERLAP_S,
    quiet_rms: float = QUIET_RMS,
) -> List[Tuple[int, int]]:
    """Return ``[start, end)`` sample ranges with a trailing overlap.

    Args:
        sample_count: Total samples in the session file.
        sample_rate: Sample rate of those samples.
        audio: Optional int16 samples used for quiet-cut scanning. When omitted
            the ranges are hard-cut at ``max_sec``.
        target_sec: Minimum duration before scanning for a quiet cut.
        max_sec: Hard maximum window duration.
        quiet_window_s: Required quiet duration.
        overlap_s: Samples of the previous window reused at the next start.
        quiet_rms: RMS threshold in int16 units.

    Returns:
        Inclusive-start exclusive-end ranges covering ``[0, sample_count)``.
    """
    rate = max(1, int(sample_rate))
    total = max(0, int(sample_count))
    if total <= 0:
        return []
    overlap_n = max(0, int(round(float(overlap_s) * rate)))
    position = 0
    ranges: List[Tuple[int, int]] = []
    while position < total:
        remaining = audio[position:] if audio is not None else None
        remaining_n = total - position
        cut = None
        if remaining is not None and remaining.size:
            cut = find_cut_point(
                remaining,
                rate,
                target_sec=target_sec,
                max_sec=max_sec,
                quiet_rms=quiet_rms,
                quiet_window_s=quiet_window_s,
            )
        if cut is None:
            cut = min(remaining_n, int(round(max_sec * rate)))
        cut = max(1, min(int(cut), remaining_n))
        end = position + cut
        ranges.append((position, end))
        if end >= total:
            break
        nxt = end - overlap_n
        if nxt <= position:
            nxt = end
        position = nxt
    return ranges


def drop_overlapped_prefix(
    segments: Sequence[TranscriptSegment],
    keep_from_s: float,
) -> List[TranscriptSegment]:
    """Drop segments that start before ``keep_from_s`` (overlap region).

    The later window keeps the overlapping tail; the earlier window's
    overlapping prefix is discarded by the caller before extend.

    Args:
        segments: Decoded segments for one window.
        keep_from_s: Meeting-clock time at which this window becomes
            authoritative.

    Returns:
        Segments whose ``start_s`` is at or after ``keep_from_s``.
    """
    threshold = float(keep_from_s)
    return [seg for seg in segments if float(seg.end_s) > threshold]


def offline_segment_id(
    meeting_id: str, channel: str, start_s: float, ordinal: int
) -> str:
    """Stable id for one offline-pass segment."""
    raw = (
        f"{meeting_id}:offline:{channel}:{int(round(start_s * 1000))}:{ordinal}"
    ).encode("utf-8")
    return f"sg_{hashlib.sha256(raw).hexdigest()[:20]}"


def session_origin_s(spool_dir: str, channel: str) -> float:
    """Meeting-clock time of session WAV sample 0."""
    meta = load_session_meta(session_meta_path(spool_dir, channel))
    if meta is not None:
        return float(meta.get("origin_s") or 0.0)
    return 0.0


def transcribe_session_audio(
    model: Any,
    frames: np.ndarray,
    sample_rate: int,
    *,
    meeting_id: str,
    channel: str,
    origin_s: float = 0.0,
    language: Optional[str] = None,
    beam_size: int = 5,
    target_sec: float = OFFLINE_TARGET_SEC,
    max_sec: float = OFFLINE_MAX_SEC,
    overlap_s: float = OFFLINE_OVERLAP_S,
) -> List[TranscriptSegment]:
    """Silence-split ``frames`` and decode sequentially with prior-text context.

    Args:
        model: faster-whisper ``WhisperModel`` (``transcribe`` method).
        frames: Mono int16 session audio.
        sample_rate: Sample rate of ``frames``.
        meeting_id: Owning meeting id (for stable segment ids).
        channel: ``mic`` or ``loopback``.
        origin_s: Meeting-clock time of sample 0.
        language: Optional ISO-639-1 language pin.
        beam_size: Whisper beam size (offline always uses quality settings).
        target_sec: Quiet-cut target duration.
        max_sec: Hard maximum window.
        overlap_s: Cross-window overlap in seconds.

    Returns:
        Meeting-clock timestamped segments covering the session.
    """
    frames = np.asarray(frames, dtype=np.int16).reshape(-1)
    if frames.size == 0 or model is None:
        return []
    from meeting.asr.engine import DRAFT_PROMPT_WORDS

    ranges = offline_cut_ranges(
        frames.size,
        sample_rate,
        frames,
        target_sec=target_sec,
        max_sec=max_sec,
        overlap_s=overlap_s,
    )
    prompt_words: List[str] = []
    collected: List[TranscriptSegment] = []
    keep_from_s = float(origin_s)
    for start, end in ranges:
        window = frames[start:end]
        audio = prepare_for_whisper(window, sample_rate)
        if audio.size == 0:
            continue
        window_origin = float(origin_s) + start / float(sample_rate)
        prompt = " ".join(prompt_words[-DRAFT_PROMPT_WORDS:]).strip() or None
        try:
            whisper_segments, _info = model.transcribe(
                audio,
                beam_size=int(beam_size),
                vad_filter=True,
                word_timestamps=False,
                language=language,
                # Same as live draft: prompt-tail continuity, no Whisper
                # auto-feedback. True+long-prompt skipped most of IN1009.
                condition_on_previous_text=False,
                initial_prompt=prompt,
            )
        except Exception:
            logger.exception(
                "Offline ASR window failed (%s %.1f-%.1fs)",
                channel, window_origin,
                window_origin + (end - start) / float(sample_rate),
            )
            continue
        decoded: List[TranscriptSegment] = []
        for ordinal, seg in enumerate(whisper_segments):
            text = (seg.text or "").strip()
            if not text:
                continue
            start_s = window_origin + float(seg.start)
            end_s = window_origin + float(seg.end)
            decoded.append(TranscriptSegment(
                segment_id=offline_segment_id(
                    meeting_id, channel, start_s, ordinal
                ),
                meeting_id=meeting_id,
                chunk_id=None,
                channel=channel,
                start_s=start_s,
                end_s=end_s,
                text=text,
            ))
        kept = drop_overlapped_prefix(decoded, keep_from_s)
        if kept:
            # Later window owns the overlap: drop earlier segments that start
            # at or after this window's origin so we do not double-count.
            collected = [
                seg for seg in collected
                if float(seg.start_s) < window_origin - 1e-6
            ]
        collected.extend(kept)
        for seg in kept:
            prompt_words.extend(seg.text.split())
        keep_from_s = float(origin_s) + end / float(sample_rate)
    return collected


def load_channel_session(
    spool_dir: str,
    channel: str,
    chunks: Optional[Sequence[Dict[str, Any]]] = None,
) -> Tuple[Optional[np.ndarray], int, float]:
    """Load one channel's 16 kHz session audio, stitching chunks if needed.

    Returns:
        ``(frames, sample_rate, origin_s)``. ``frames`` is None when missing.
    """
    wav_path = resolve_session_wav(spool_dir, channel, chunks)
    if not wav_path:
        return None, WHISPER_SAMPLE_RATE, 0.0
    try:
        frames, rate = load_wav_int16(wav_path)
    except Exception:
        logger.exception("Failed to load session WAV %s", wav_path)
        return None, WHISPER_SAMPLE_RATE, 0.0
    origin = session_origin_s(spool_dir, channel)
    if origin == 0.0 and chunks:
        channel_chunks = [
            chunk for chunk in chunks
            if str(chunk.get("channel") or "") == channel
        ]
        if channel_chunks:
            origin = min(float(chunk.get("start_s") or 0.0) for chunk in channel_chunks)
    return frames, rate, origin


TranscribeFn = Callable[..., List[TranscriptSegment]]


def transcribe_meeting_sessions(
    model: Any,
    spool_dir: str,
    meeting_id: str,
    chunks: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    language: Optional[str] = None,
    channels: Iterable[str] = CHANNELS,
    transcribe_fn: Optional[TranscribeFn] = None,
) -> List[TranscriptSegment]:
    """Run the offline pass over every capture channel and merge by time.

    Args:
        model: faster-whisper model.
        spool_dir: Meeting spool directory.
        meeting_id: Owning meeting id.
        chunks: Optional chunk rows for the session-WAV fallback.
        language: Optional language pin.
        channels: Channels to decode.
        transcribe_fn: Injectable decoder (tests); defaults to
            :func:`transcribe_session_audio`.

    Returns:
        All channels' segments sorted by ``start_s``.
    """
    decoder = transcribe_fn or transcribe_session_audio
    merged: List[TranscriptSegment] = []
    for channel in channels:
        if not os.path.isdir(spool_dir) and not chunks:
            continue
        frames, rate, origin = load_channel_session(spool_dir, channel, chunks)
        if frames is None or frames.size == 0:
            logger.info("No session audio for offline ASR on channel %s", channel)
            continue
        decoded = decoder(
            model,
            frames,
            rate,
            meeting_id=meeting_id,
            channel=channel,
            origin_s=origin,
            language=language,
        )
        merged.extend(decoded)
        logger.info(
            "Offline ASR %s: %d segments from %.1fs of audio",
            channel, len(decoded), frames.size / float(rate or WHISPER_SAMPLE_RATE),
        )
    merged.sort(key=lambda seg: (float(seg.start_s), seg.channel, seg.segment_id))
    return merged
