"""Derived content capabilities for persisted Meeting Mode sessions."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from meeting.state.patches import MAX_NAME_LEN

logger = logging.getLogger(__name__)


def _meeting_state_dict(meeting: Dict[str, Any]) -> Dict[str, Any]:
    """Return a meeting's persisted state mapping, when one is present."""
    state = meeting.get("state")
    if isinstance(state, dict):
        return state
    raw = meeting.get("state_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def meeting_display_title(meeting: Dict[str, Any]) -> str:
    """Return the best persisted label available for a history row.

    Prefers the SQL title, then ``state.title``, then the generated topic.
    Does not invent a fallback string — callers that need one use
    :func:`fallback_meeting_title`.
    """
    title = str(meeting.get("title") or "").strip()
    if title:
        return title
    state = _meeting_state_dict(meeting)
    state_title = str(state.get("title") or "").strip()
    if state_title:
        return state_title
    topic = state.get("topic")
    if isinstance(topic, dict):
        return str(topic.get("current") or "").strip()
    return ""


def fallback_meeting_title(meeting: Dict[str, Any]) -> str:
    """Return a UI label, using Failed/Untitled only when nothing is stored."""
    display = meeting_display_title(meeting)
    if display:
        return display
    status = str(meeting.get("status") or "").lower()
    return "Failed meeting" if status == "failed" else "Untitled meeting"


def untitled_title_from_topic(state: Dict[str, Any]) -> str:
    """Return ``topic.current`` when the meeting title is still blank."""
    if str(state.get("title") or "").strip():
        return ""
    topic = state.get("topic")
    if not isinstance(topic, dict):
        return ""
    return str(topic.get("current") or "").strip()[:MAX_NAME_LEN]


def build_untitled_title_ops(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Host/system ``set_title`` from topic when the meeting is still untitled."""
    text = untitled_title_from_topic(state)
    if not text:
        return []
    return [{"op": "set_title", "text": text}]


def meeting_insights_pill(
    meeting: Dict[str, Any],
) -> Optional[Tuple[str, str]]:
    """Return the compact insights pill ``(label, tone)`` for a history row."""
    if str(meeting.get("status") or "").lower() == "failed":
        return ("Failed start", "warning")
    if bool((meeting.get("content_summary") or {}).get("is_empty", False)):
        return ("Empty", "warning")
    try:
        from meeting.state.schema import finalization_from_meeting_row

        fin = finalization_from_meeting_row(meeting)
    except Exception:
        logger.debug(
            "Could not derive insights pill for a past meeting", exc_info=True
        )
        return None
    return fin.history_pill(
        meeting_status=str(meeting.get("status") or "ended")
    )


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
