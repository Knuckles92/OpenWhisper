"""Derived content capabilities for persisted Meeting Mode sessions."""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def summarize_meeting_content(repository: Any, meeting_id: str) -> Dict[str, Any]:
    """Return honest audio/transcript capabilities for one meeting.

    The values are derived from durable rows rather than finalization status:
    cloud insights may be disabled or fail independently of whether anything
    was actually recorded.
    """
    chunks = []
    segments = []
    try:
        chunks = list(repository.get_audio_chunks(meeting_id) or [])
    except Exception:
        logger.exception("Could not inspect audio for meeting %s", meeting_id)
    try:
        segments = list(repository.get_segments(meeting_id) or [])
    except Exception:
        logger.exception("Could not inspect transcript for meeting %s", meeting_id)

    audio_chunks = [
        chunk for chunk in chunks
        if float(chunk.get("duration_s") or 0.0) > 0.0
    ]
    transcript_segments = [
        segment for segment in segments
        if str(segment.get("text") or "").strip()
    ]
    has_audio = bool(audio_chunks)
    has_loopback_audio = any(
        str(chunk.get("channel") or "") == "loopback"
        for chunk in audio_chunks
    )
    has_transcript = bool(transcript_segments)
    return {
        "has_audio": has_audio,
        "has_loopback_audio": has_loopback_audio,
        "has_transcript": has_transcript,
        "is_empty": not has_audio and not has_transcript,
        "audio_chunks": len(audio_chunks),
        "transcript_segments": len(transcript_segments),
        "can_rerun_speakers": has_loopback_audio,
    }
