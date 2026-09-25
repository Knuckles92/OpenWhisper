"""Shared loopback speaker assignment for live chunks and offline sessions.

The live chunk path, the end-of-meeting re-decode, and the Past Meetings
retry all slice loopback audio per segment and hand it to an
``OnlineDiarizer``. Keeping that in one place keeps the sample indexing and
the post-recluster label refresh identical across the three.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)


def assign_from_frames(
    diarizer: Any,
    segments: Sequence[Any],
    frames: Any,
    rate: int,
    origin_s: float,
    *,
    on_unlabeled: Optional[Callable[[], None]] = None,
) -> List[Any]:
    """Label segments by slicing ``frames`` and running the diarizer.

    Args:
        diarizer: An ``OnlineDiarizer`` (anything with ``assign``).
        segments: ``TranscriptSegment`` objects; labeled in place.
        frames: int16 audio covering the segments.
        rate: Sample rate of ``frames`` in Hz.
        origin_s: Meeting time of ``frames[0]``.
        on_unlabeled: Called when the diarizer returns no label.

    Returns:
        The segments that received a diarizer label, in input order.
    """
    from meeting.asr.audio import prepare_for_whisper

    labeled: List[Any] = []
    for seg in segments:
        start = max(0, int(round((seg.start_s - origin_s) * rate)))
        end = min(len(frames), int(round((seg.end_s - origin_s) * rate)))
        if end <= start:
            continue
        try:
            audio = prepare_for_whisper(frames[start:end], rate)
            participant_id = diarizer.assign(seg, audio, 16000)
        except Exception:
            logger.exception("Diarizer assignment failed for %s", seg.segment_id)
            participant_id = None
        if participant_id:
            seg.speaker_participant_id = participant_id
            seg.speaker_source = "diarizer"
            labeled.append(seg)
        elif on_unlabeled is not None:
            on_unlabeled()
    return labeled


def refresh_labels(diarizer: Any, segments: Sequence[Any]) -> None:
    """Re-read labels a re-cluster may have changed after ``assign`` returned.

    Segments that are not persisted yet cannot receive relabel ops, so a
    batch caller refreshes them from the diarizer before committing.

    Args:
        diarizer: The ``OnlineDiarizer`` that labeled ``segments``.
        segments: Segments previously labeled by that diarizer.
    """
    current_label = getattr(diarizer, "current_label", None)
    if not callable(current_label):
        return
    for seg in segments:
        try:
            participant_id = current_label(seg.segment_id)
        except Exception:
            logger.exception("Diarizer label refresh failed for %s", seg.segment_id)
            continue
        if participant_id:
            seg.speaker_participant_id = participant_id
