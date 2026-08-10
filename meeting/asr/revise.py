"""Bounded rolling ASR revision helpers for Meeting Mode.

Draft ASR commits each quiet-cut chunk once. A reviser then re-decodes the
trailing :data:`REVISION_WINDOW_S` of audio per channel, matches new Whisper
segments onto existing evidence anchors by time IoU, and returns a plan the
repository can apply without unbounded full-meeting retranscription.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from meeting.asr.audio import WHISPER_SAMPLE_RATE, load_wav_int16, prepare_for_whisper
from meeting.interfaces import TranscriptSegment

logger = logging.getLogger(__name__)

#: Trailing audio horizon re-decoded after each draft chunk commit.
REVISION_WINDOW_S = 45.0

#: Audio decoded before the mutable window boundary. Without this lead-in, a
#: Whisper segment crossing the 45-second cutoff can be re-decoded from its
#: middle and overwrite the original row with only the trailing phrase.
REVISION_CONTEXT_S = 5.0

#: Minimum IoU required to reuse an existing segment id.
MIN_MATCH_IOU = 0.25

#: Characters of finalized transcript fed to Whisper as ``initial_prompt``.
INITIAL_PROMPT_CHARS = 224


@dataclass
class RevisePlan:
    """Outcome of matching a window re-decode onto existing segments."""

    upserts: List[TranscriptSegment]
    remove_ids: List[str]
    window_start_s: float
    window_end_s: float


def revision_window(frontier_s: float, window_s: float = REVISION_WINDOW_S) -> Tuple[float, float]:
    """Return ``[start, end)`` meeting-clock bounds for a revise pass."""
    end_s = max(0.0, float(frontier_s))
    start_s = max(0.0, end_s - max(0.0, float(window_s)))
    return start_s, end_s


def interval_iou(
    a_start: float, a_end: float, b_start: float, b_end: float
) -> float:
    """Intersection-over-union for two half-open time intervals."""
    start = max(float(a_start), float(b_start))
    end = min(float(a_end), float(b_end))
    inter = max(0.0, end - start)
    if inter <= 0.0:
        return 0.0
    union = (
        max(float(a_end), float(b_end)) - min(float(a_start), float(b_start))
    )
    if union <= 0.0:
        return 0.0
    return inter / union


def select_chunks_for_window(
    chunks: Sequence[Dict[str, Any]],
    channel: str,
    window_start_s: float,
    window_end_s: float,
) -> List[Dict[str, Any]]:
    """Chunks on ``channel`` whose audio overlaps ``[window_start, window_end)``."""
    selected: List[Dict[str, Any]] = []
    for chunk in chunks:
        if chunk.get("channel") != channel:
            continue
        start = float(chunk.get("start_s") or 0.0)
        duration = float(chunk.get("duration_s") or 0.0)
        end = start + duration
        if end <= window_start_s or start >= window_end_s:
            continue
        if chunk.get("asr_status") not in (None, "done", "processing"):
            # Still allow pending/failed files if present on disk; caller loads.
            pass
        selected.append(chunk)
    selected.sort(key=lambda c: (float(c.get("start_s") or 0.0), int(c.get("seq") or 0)))
    return selected


def stitch_window_audio(
    chunks: Sequence[Dict[str, Any]],
    window_start_s: float,
    window_end_s: float,
) -> Tuple[np.ndarray, float]:
    """Load and concatenate chunk WAVs clipped to the revision window.

    Returns:
        Float32 Whisper audio and the meeting-clock time of sample 0. Empty
        audio when nothing usable is on disk.
    """
    if window_end_s <= window_start_s:
        return np.zeros(0, dtype=np.float32), window_start_s

    pieces: List[np.ndarray] = []
    cursor = float(window_start_s)
    actual_start = float(window_start_s)

    for chunk in chunks:
        chunk_start = float(chunk.get("start_s") or 0.0)
        duration = float(chunk.get("duration_s") or 0.0)
        chunk_end = chunk_start + duration
        overlap_start = max(window_start_s, chunk_start)
        overlap_end = min(window_end_s, chunk_end)
        if overlap_end <= overlap_start:
            continue

        path = chunk.get("file_path")
        if not path:
            continue
        try:
            frames, rate = load_wav_int16(str(path))
        except Exception:
            logger.exception("Failed to load chunk %s for revise stitch", chunk.get("id"))
            continue

        if cursor < overlap_start:
            gap_s = overlap_start - cursor
            gap_n = max(0, int(round(gap_s * WHISPER_SAMPLE_RATE)))
            if gap_n:
                pieces.append(np.zeros(gap_n, dtype=np.float32))
            cursor = overlap_start

        rel_start = overlap_start - chunk_start
        rel_end = overlap_end - chunk_start
        i0 = max(0, int(round(rel_start * rate)))
        i1 = min(len(frames), int(round(rel_end * rate)))
        if i1 <= i0:
            continue
        audio = prepare_for_whisper(frames[i0:i1], rate)
        if audio.size == 0:
            continue
        if not pieces:
            actual_start = overlap_start
        pieces.append(audio)
        cursor = overlap_end

    if not pieces:
        return np.zeros(0, dtype=np.float32), window_start_s
    return np.concatenate(pieces).astype(np.float32, copy=False), actual_start


def build_initial_prompt(prior_segments: Sequence[Dict[str, Any]]) -> str:
    """Trailing finalized text used as Whisper ``initial_prompt``."""
    parts: List[str] = []
    for seg in prior_segments:
        text = (seg.get("text") or "").strip()
        if text:
            parts.append(text)
    joined = " ".join(parts).strip()
    if len(joined) <= INITIAL_PROMPT_CHARS:
        return joined
    return joined[-INITIAL_PROMPT_CHARS:].lstrip()


def revise_segment_id(
    meeting_id: str, channel: str, start_s: float, ordinal: int
) -> str:
    """Stable id for a newly inserted revise-window segment."""
    raw = (
        f"{meeting_id}:revise:{channel}:{int(round(start_s * 1000))}:{ordinal}"
    ).encode("utf-8")
    return f"sg_{hashlib.sha256(raw).hexdigest()[:20]}"


def match_segments(
    existing: Sequence[Dict[str, Any]],
    decoded: Sequence[TranscriptSegment],
    *,
    min_iou: float = MIN_MATCH_IOU,
) -> RevisePlan:
    """Greedy IoU match of decoded segments onto existing rows.

    Matched rows keep their ``sg_`` ids (text/times update). Unmatched decoded
    segments become inserts. Unmatched existing rows that are not
    ``speaker_pinned`` are listed for deletion; pinned rows are kept untouched.
    """
    old_items = [
        {
            "id": str(row["id"]),
            "start_s": float(row["start_s"]),
            "end_s": float(row["end_s"]),
            "pinned": bool(row.get("speaker_pinned")),
            "speaker_participant_id": row.get("speaker_participant_id"),
            "speaker_source": row.get("speaker_source") or "channel",
            "chunk_id": row.get("chunk_id"),
            "channel": row.get("channel"),
            "meeting_id": row.get("meeting_id"),
            "text": row.get("text") or "",
        }
        for row in existing
    ]
    new_items = list(decoded)
    pairs: List[Tuple[float, int, int]] = []
    for oi, old in enumerate(old_items):
        for ni, new in enumerate(new_items):
            score = interval_iou(old["start_s"], old["end_s"], new.start_s, new.end_s)
            if score >= min_iou:
                pairs.append((score, oi, ni))
    pairs.sort(reverse=True)

    used_old: set = set()
    used_new: set = set()
    upserts: List[TranscriptSegment] = []

    for _score, oi, ni in pairs:
        if oi in used_old or ni in used_new:
            continue
        used_old.add(oi)
        used_new.add(ni)
        old = old_items[oi]
        new = new_items[ni]
        upserts.append(TranscriptSegment(
            segment_id=old["id"],
            meeting_id=new.meeting_id,
            chunk_id=old["chunk_id"] if old["chunk_id"] is not None else new.chunk_id,
            channel=new.channel,
            start_s=new.start_s,
            end_s=new.end_s,
            text=new.text,
            speaker_participant_id=(
                old["speaker_participant_id"]
                if old["pinned"] or old["speaker_participant_id"]
                else new.speaker_participant_id
            ),
            speaker_source=(
                old["speaker_source"]
                if old["pinned"] or old["speaker_participant_id"]
                else new.speaker_source
            ),
            speaker_pinned=old["pinned"],
        ))

    for ni, new in enumerate(new_items):
        if ni in used_new:
            continue
        upserts.append(new)

    remove_ids: List[str] = []
    for oi, old in enumerate(old_items):
        if oi in used_old:
            continue
        if old["pinned"]:
            continue
        remove_ids.append(old["id"])

    window_start = min(
        ([o["start_s"] for o in old_items] + [n.start_s for n in new_items] + [0.0])
    )
    window_end = max(
        ([o["end_s"] for o in old_items] + [n.end_s for n in new_items] + [0.0])
    )
    return RevisePlan(
        upserts=upserts,
        remove_ids=remove_ids,
        window_start_s=window_start,
        window_end_s=window_end,
    )


def overlaps_window(
    start_s: float, end_s: float, window_start_s: float, window_end_s: float
) -> bool:
    """True when ``[start, end)`` overlaps ``[window_start, window_end)``."""
    return float(end_s) > float(window_start_s) and float(start_s) < float(window_end_s)
