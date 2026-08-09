"""Plain timestamped transcript export, plus formatting helpers shared by
the other exporters.

All exporters in :mod:`meeting.export` are pure functions over three
plain-dict inputs:

* ``meeting``: a row dict from ``SqlMeetingRepository.get_meeting``.
* ``state``: a ``MeetingState.to_dict()`` document.
* ``segments``: row dicts from ``SqlMeetingRepository.get_segments``,
  ordered by ``start_s``.

Nothing here performs I/O or imports beyond the standard library, keeping
the module extractable with the rest of the ``meeting`` package.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Fallback speaker labels when a segment has no resolvable participant.
FALLBACK_SPEAKER_MIC = "Me"
FALLBACK_SPEAKER_LOOPBACK = "Others"


def parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, returning None on any failure.

    Args:
        value: Raw timestamp value — usually ``datetime.isoformat()`` output
            stored by the repository; may be None or malformed.

    Returns:
        The parsed ``datetime``, or None when ``value`` is missing or is not
        a valid ISO-8601 string.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_clock(seconds: float) -> str:
    """Format a meeting-clock offset as zero-padded ``hh:mm:ss``.

    Args:
        seconds: Offset in meeting seconds; negative values clamp to zero.

    Returns:
        A ``hh:mm:ss`` string; the hour field widens past two digits rather
        than wrapping for very long recordings.
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_mmss(seconds: float) -> str:
    """Format a meeting-clock offset as ``mm:ss``.

    Args:
        seconds: Offset in meeting seconds; negative values clamp to zero.

    Returns:
        A ``mm:ss`` string; the minute field exceeds 59 for offsets past one
        hour instead of wrapping.
    """
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def resolve_title(meeting: Dict[str, Any], state: Dict[str, Any]) -> str:
    """Pick the best display title for an export.

    Preference order: the state document's title, the meeting row's title,
    then ``Meeting <date>`` derived from ``started_at``, then ``Meeting``.

    Args:
        meeting: Meeting row dict (``get_meeting`` shape).
        state: ``MeetingState.to_dict()`` document.

    Returns:
        A non-empty title string.
    """
    title = (meeting.get("title") or "").strip() or (state.get("title") or "").strip()
    if title:
        return title
    started = parse_iso(meeting.get("started_at"))
    if started is not None:
        return f"Meeting {started.strftime('%Y-%m-%d')}"
    return "Meeting"


def format_meeting_date(meeting: Dict[str, Any]) -> Optional[str]:
    """Human-readable start stamp (``YYYY-MM-DD HH:MM``) for the header line.

    Args:
        meeting: Meeting row dict with an optional ``started_at`` ISO string.

    Returns:
        The formatted stamp, the raw value when it is present but unparsable,
        or None when there is no start time at all.
    """
    started = parse_iso(meeting.get("started_at"))
    if started is not None:
        return started.strftime("%Y-%m-%d %H:%M")
    raw = meeting.get("started_at")
    return str(raw) if raw else None


def participant_names(state: Dict[str, Any]) -> List[str]:
    """Participant display names with the host ("me") listed first.

    Args:
        state: ``MeetingState.to_dict()`` document.

    Returns:
        Non-empty display names; the ``me`` participant leads, everyone else
        follows in insertion order.
    """
    participants = list((state.get("participants") or {}).values())
    participants.sort(key=lambda p: 0 if p.get("kind") == "me" else 1)
    return [
        name for name in (
            (p.get("display_name") or "").strip() for p in participants
        ) if name
    ]


def speaker_name(segment: Dict[str, Any],
                 participants: Dict[str, Dict[str, Any]]) -> str:
    """Resolve the display name for a transcript segment's speaker.

    Args:
        segment: Segment row dict (``get_segments`` shape).
        participants: The state document's participant map (id -> dict).

    Returns:
        The participant's display name when ``speaker_participant_id``
        resolves, otherwise ``Me`` for the mic channel or ``Others`` for
        loopback.
    """
    pid = segment.get("speaker_participant_id")
    if pid:
        participant = participants.get(pid)
        if participant:
            name = (participant.get("display_name") or "").strip()
            if name:
                return name
    if segment.get("channel") == "mic":
        return FALLBACK_SPEAKER_MIC
    return FALLBACK_SPEAKER_LOOPBACK


def transcript_lines(segments: List[Dict[str, Any]],
                     participants: Dict[str, Dict[str, Any]]) -> List[str]:
    """Render segments as ``[hh:mm:ss] Speaker: text`` lines.

    Args:
        segments: Segment row dicts ordered by ``start_s``.
        participants: The state document's participant map (id -> dict).

    Returns:
        One line per segment with non-empty text, in input order.
    """
    lines: List[str] = []
    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        stamp = format_clock(float(segment.get("start_s") or 0.0))
        lines.append(f"[{stamp}] {speaker_name(segment, participants)}: {text}")
    return lines


def export_transcript_txt(meeting: Dict[str, Any], state: Dict[str, Any],
                          segments: List[Dict[str, Any]]) -> str:
    """Render the meeting as a plain timestamped transcript.

    The document opens with a small header (title, date, participant names)
    followed by one ``[hh:mm:ss] Speaker: text`` line per segment.

    Args:
        meeting: Meeting row dict (``get_meeting`` shape).
        state: ``MeetingState.to_dict()`` document.
        segments: Segment row dicts ordered by ``start_s``.

    Returns:
        The complete transcript text, ending with a newline.
    """
    header = [resolve_title(meeting, state)]
    date = format_meeting_date(meeting)
    if date:
        header.append(date)
    names = participant_names(state)
    if names:
        header.append("Participants: " + ", ".join(names))
    lines = transcript_lines(segments, state.get("participants") or {})
    body = "\n".join(lines) if lines else "(no transcript)"
    return "\n".join(header) + "\n\n" + body + "\n"
