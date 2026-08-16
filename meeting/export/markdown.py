"""Markdown export: the dashboard rendered as a clean shareable document.

Layout: title, metadata line (date, duration, participants), topic with
history, rolling summary, one section per dashboard card, questions (open
and resolved), and the full timestamped transcript. Empty sections and
removed items are skipped. Evidence segment ids are deliberately not
rendered — this is the human artifact; the JSON export carries evidence.

Pure functions, standard library only.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from meeting.export.transcript_txt import (
    format_clock,
    format_meeting_date,
    format_mmss,
    parse_iso,
    participant_names,
    resolve_title,
    transcript_lines,
)

logger = logging.getLogger(__name__)

#: Card keys in render order with their section headings. The note taker's
#: running minutes come first among the cards: they read as the narrative
#: record the structured cards then back up.
_CARD_SECTIONS = (
    ("live_notes", "Meeting Notes"),
    ("key_points", "Key Points"),
    ("decisions", "Decisions"),
    ("action_items", "Action Items"),
    ("risks", "Risks"),
    ("timeline", "Timeline"),
    ("user_notes", "Notes"),
)


def export_markdown(meeting: Dict[str, Any], state: Dict[str, Any],
                    segments: List[Dict[str, Any]]) -> str:
    """Render the meeting dashboard and transcript as a Markdown document.

    Args:
        meeting: Meeting row dict (``get_meeting`` shape).
        state: ``MeetingState.to_dict()`` document.
        segments: Segment row dicts ordered by ``start_s``.

    Returns:
        The complete Markdown document, ending with a newline.
    """
    participants = state.get("participants") or {}
    out: List[str] = [f"# {resolve_title(meeting, state)}"]

    metadata = _metadata_line(meeting, state, segments)
    if metadata:
        out += ["", metadata]

    _append_topic(out, state)
    _append_summary(out, state)

    for card, heading in _CARD_SECTIONS:
        items = _live_items(state, card)
        if not items:
            continue
        out += ["", f"## {heading}", ""]
        if card == "action_items":
            out.extend(_action_item_lines(items, participants))
        elif card == "risks":
            out.extend(_risk_lines(items))
        elif card == "timeline":
            out.extend(_timeline_lines(items))
        elif card == "key_points":
            out.extend(_key_point_lines(items, segments))
        elif card == "live_notes":
            out.extend(_live_note_lines(items))
        else:
            out.extend(f"- {item['text'].strip()}" for item in items)

    _append_questions(out, state, participants)

    lines = transcript_lines(segments, participants)
    if lines:
        out += ["", "### Transcript", ""]
        # Trailing double space forces a Markdown hard line break per segment.
        out.extend(line + "  " for line in lines)

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def _metadata_line(meeting: Dict[str, Any], state: Dict[str, Any],
                   segments: List[Dict[str, Any]]) -> str:
    """Single metadata line: date, duration, and participant names."""
    parts: List[str] = []
    date = format_meeting_date(meeting)
    if date:
        parts.append(date)
    duration_s = _duration_s(meeting, segments)
    if duration_s is not None:
        parts.append(f"Duration {format_clock(duration_s)}")
    names = participant_names(state)
    if names:
        parts.append("Participants: " + ", ".join(names))
    return " · ".join(parts)


def _duration_s(meeting: Dict[str, Any],
                segments: List[Dict[str, Any]]) -> Optional[float]:
    """Meeting duration: the segments' max ``end_s``, else ended-started."""
    ends = [float(s.get("end_s") or 0.0) for s in segments]
    if ends and max(ends) > 0:
        return max(ends)
    started = parse_iso(meeting.get("started_at"))
    ended = parse_iso(meeting.get("ended_at"))
    if started is not None and ended is not None:
        span = (ended - started).total_seconds()
        if span >= 0:
            return span
    return None


# ---------------------------------------------------------------------------
# Topic / summary
# ---------------------------------------------------------------------------

def _append_topic(out: List[str], state: Dict[str, Any]) -> None:
    """Append the current topic and its earlier revisions."""
    topic = state.get("topic") or {}
    current = (topic.get("current") or "").strip()
    if not current:
        return
    out += ["", "## Topic", "", current]
    # The last history entry is the current topic; earlier entries are the
    # revision trail.
    previous = (topic.get("history") or [])[:-1]
    rendered: List[str] = []
    for entry in previous:
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        ts = parse_iso(entry.get("ts"))
        stamp = f" ({ts.strftime('%H:%M')})" if ts else ""
        rendered.append(f"- {text}{stamp}")
    if rendered:
        out += ["", "Previously:", ""]
        out.extend(rendered)


def _append_summary(out: List[str], state: Dict[str, Any]) -> None:
    summary = (state.get("rolling_summary") or "").strip()
    if summary:
        out += ["", "## Summary", "", summary]


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def _live_items(state: Dict[str, Any], card: str) -> List[Dict[str, Any]]:
    """Items on a card that are not removed and have non-empty text."""
    items = (state.get("cards") or {}).get(card) or []
    return [
        item for item in items
        if item.get("status") != "removed" and (item.get("text") or "").strip()
    ]


def _action_item_lines(items: List[Dict[str, Any]],
                       participants: Dict[str, Dict[str, Any]]) -> List[str]:
    """Action items grouped Mine / Others / Unassigned when owners are known.

    Ownership comes from ``data.owner_participant_id``: an owner whose
    participant kind is ``me`` is Mine; any other resolvable owner is Others
    (with the owner's name appended); unresolvable or absent owners are
    Unassigned. With no known owners at all, the list stays flat.
    """
    mine: List[str] = []
    others: List[str] = []
    unassigned: List[str] = []
    for item in items:
        text = item["text"].strip()
        owner_id = (item.get("data") or {}).get("owner_participant_id")
        # item["data"] contents come straight from the model and are only
        # validated to be a dict, so an unhashable value here would abort the
        # whole document.
        owner = participants.get(owner_id) if isinstance(owner_id, str) else None
        if owner is None:
            unassigned.append(f"- {text}")
        elif owner.get("kind") == "me":
            mine.append(f"- {text}")
        else:
            name = (owner.get("display_name") or "").strip() or "Unknown"
            others.append(f"- {text} — {name}")

    if not (mine or others):
        return unassigned
    lines: List[str] = []
    for label, group in (("Mine", mine), ("Others", others),
                         ("Unassigned", unassigned)):
        if not group:
            continue
        if lines:
            lines.append("")
        lines += [f"**{label}**", ""]
        lines.extend(group)
    return lines


def _risk_lines(items: List[Dict[str, Any]]) -> List[str]:
    """Risk bullets, prefixed with ``data.severity`` when present."""
    lines: List[str] = []
    for item in items:
        text = item["text"].strip()
        severity = str((item.get("data") or {}).get("severity") or "").strip()
        if severity:
            lines.append(f"- **[{severity}]** {text}")
        else:
            lines.append(f"- {text}")
    return lines


def _timeline_lines(items: List[Dict[str, Any]]) -> List[str]:
    """Timeline bullets stamped ``[mm:ss]`` from ``data.start_s``."""
    lines: List[str] = []
    for item in items:
        text = item["text"].strip()
        start_s = (item.get("data") or {}).get("start_s")
        if isinstance(start_s, (int, float)) and not isinstance(start_s, bool):
            lines.append(f"- [{format_mmss(float(start_s))}] {text}")
        else:
            lines.append(f"- {text}")
    return lines


def _live_note_lines(items: List[Dict[str, Any]]) -> List[str]:
    """Note-taker blocks in page order: ``**[mm:ss] heading** — body``.

    Heading and stamp come from ``data.heading`` / ``data.start_s``; either
    may be absent, in which case the bullet degrades gracefully.
    """
    lines: List[str] = []
    for item in items:
        text = " ".join(item["text"].split())
        data = item.get("data") or {}
        heading = str(data.get("heading") or "").strip()
        start_s = data.get("start_s")
        stamp = (
            format_mmss(float(start_s))
            if isinstance(start_s, (int, float))
            and not isinstance(start_s, bool)
            else ""
        )
        prefix_parts = [part for part in (stamp, heading) if part]
        prefix = f"**{' '.join(prefix_parts)}** — " if prefix_parts else ""
        lines.append(f"- {prefix}{text}")
    return lines


def _key_point_lines(items: List[Dict[str, Any]],
                     segments: List[Dict[str, Any]]) -> List[str]:
    """Key-point bullets with an optional ``[mm:ss]`` from earliest evidence.

    Timestamps make the shareable Markdown document navigable against the
    transcript without exposing raw segment ids.
    """
    by_id = {
        seg["id"]: seg
        for seg in segments
        if isinstance(seg, dict) and seg.get("id")
    }
    lines: List[str] = []
    for item in items:
        text = item["text"].strip()
        starts: List[float] = []
        for seg_id in item.get("evidence") or []:
            seg = by_id.get(seg_id)
            if seg is None:
                continue
            try:
                starts.append(float(seg.get("start_s") or 0.0))
            except (TypeError, ValueError):
                continue
        if starts:
            lines.append(f"- [{format_mmss(min(starts))}] {text}")
        else:
            lines.append(f"- {text}")
    return lines


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

def _append_questions(out: List[str], state: Dict[str, Any],
                      participants: Dict[str, Dict[str, Any]]) -> None:
    """Append open questions (with suggestions) and resolved ones (with
    answers and attribution badges). Dismissed questions are omitted."""
    questions = state.get("questions") or []
    open_qs = [q for q in questions
               if q.get("status") == "open" and (q.get("text") or "").strip()]
    resolved = [q for q in questions
                if q.get("status") == "resolved" and (q.get("text") or "").strip()]
    if not (open_qs or resolved):
        return
    out += ["", "## Questions", ""]
    for question in open_qs:
        out.append(f"- **Open:** {question['text'].strip()}")
        suggestion = (question.get("suggested_answer") or "").strip()
        if suggestion:
            confidence = question.get("suggested_confidence")
            note = (f" ({confidence:.0%} confidence)"
                    if isinstance(confidence, (int, float))
                    and not isinstance(confidence, bool) else "")
            out.append(f"  - Suggested answer{note}: {suggestion}")
    for question in resolved:
        out.append(f"- **Resolved:** {question['text'].strip()}")
        answer = (question.get("answer") or "").strip()
        if answer:
            badge = _answer_badge(question, participants)
            out.append(f"  - Answer: {answer} — _{badge}_")


def _answer_badge(question: Dict[str, Any],
                  participants: Dict[str, Dict[str, Any]]) -> str:
    """Attribution badge: 'answered from audio' or 'answered by <name>'."""
    if question.get("answer_source") == "audio":
        return "answered from audio"
    resolver = participants.get(question.get("resolved_by") or "") or {}
    name = (resolver.get("display_name") or "").strip()
    return f"answered by {name}" if name else "answered by participant"
