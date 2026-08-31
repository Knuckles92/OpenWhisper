"""Bulk export assembly for Past Meetings.

Selection criteria and document assembly over the per-meeting renderers in
:mod:`meeting.export`. Rendering stays pure (standard library only); the only
I/O is the explicit ``write_per_meeting_files`` call, which owns its output
directory. The Qt export dialog owns threading and path picking.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from meeting.export.json_export import FORMAT_VERSION, export_json
from meeting.export.markdown import export_markdown
from meeting.export.transcript_txt import export_transcript_txt, resolve_title
from meeting.time_utils import as_local_time

logger = logging.getLogger(__name__)

FORMAT_MARKDOWN = "markdown"
FORMAT_TXT = "txt"
FORMAT_JSON = "json"
EXPORT_FORMATS = (FORMAT_MARKDOWN, FORMAT_TXT, FORMAT_JSON)

_LIVE_STATUSES = frozenset({"active", "paused", "ending"})
_FILE_STEM_MAX_TITLE = 60


def list_export_meetings(repository: Any = None) -> List[Dict[str, Any]]:
    """Return past meetings with content summaries, newest first.

    Rows without a ``content_summary`` get one via
    ``summarize_meeting_content`` so downstream transcript filtering works
    from the row alone.
    """
    if repository is None:
        from meeting.persist.repository import SqlMeetingRepository

        repository = SqlMeetingRepository()
    from meeting.content import summarize_meeting_content

    meetings = []
    for row in repository.list_meetings():
        meeting = dict(row)
        if str(meeting.get("status") or "").lower() in _LIVE_STATUSES:
            continue
        if "content_summary" not in meeting:
            meeting["content_summary"] = summarize_meeting_content(
                repository, str(meeting.get("id") or "")
            )
        meetings.append(meeting)
    return meetings


def filter_export_meetings(
    meetings: List[Dict[str, Any]],
    *,
    from_dt: Optional[datetime] = None,
    to_dt: Optional[datetime] = None,
    only_with_transcript: bool = False,
) -> List[Dict[str, Any]]:
    """Apply the export criteria to past-meeting rows.

    Date bounds are naive local wall-clock values (matching a date-only
    picker); stored timestamps are converted to local time before
    comparing. Meetings whose ``started_at`` cannot be parsed are excluded
    whenever a date bound is set (their date cannot be verified), and
    meetings whose transcript availability is unknown are kept — "only with
    transcript" drops meetings known to have none.
    """
    filtered: List[Dict[str, Any]] = []
    for meeting in meetings:
        if str(meeting.get("status") or "").lower() in _LIVE_STATUSES:
            continue
        if from_dt is not None or to_dt is not None:
            started = as_local_time(meeting.get("started_at"))
            if started is None:
                continue
            started = started.replace(tzinfo=None)
            if from_dt is not None and started < from_dt:
                continue
            if to_dt is not None and started > to_dt:
                continue
        if only_with_transcript:
            summary = meeting.get("content_summary") or {}
            if isinstance(summary, dict) and summary.get("has_transcript") is False:
                continue
        filtered.append(meeting)
    return filtered


def collect_meeting_export(
    repository: Any, meeting_id: str
) -> Optional[Dict[str, Any]]:
    """Assemble the ``(meeting, state, segments)`` inputs for one renderer.

    Returns None when the meeting no longer exists (for example deleted
    while an export queue was being processed).
    """
    meeting = repository.get_meeting(meeting_id)
    if meeting is None:
        return None
    raw = meeting.get("state_json")
    if isinstance(raw, dict):
        state = raw
    else:
        parsed = _loads_or_none(raw)
        state = parsed if isinstance(parsed, dict) else {}
    segments = list(repository.get_segments(meeting_id) or [])
    return {"meeting": meeting, "state": state, "segments": segments}


def render_meeting_document(
    entry: Dict[str, Any],
    fmt: str,
    *,
    include_transcript: bool = True,
    include_intelligence: bool = True,
) -> str:
    """Render one collected entry in the requested format.

    The transcript and intelligence toggles only apply to Markdown; the txt
    format is a transcript by definition and the JSON export is the complete
    machine-readable record.
    """
    meeting = entry["meeting"]
    state = entry["state"]
    segments = entry["segments"]
    if fmt == FORMAT_JSON:
        return export_json(meeting, state, segments)
    if fmt == FORMAT_TXT:
        return export_transcript_txt(meeting, state, segments)
    return export_markdown(
        meeting,
        state,
        segments,
        include_transcript=include_transcript,
        include_intelligence=include_intelligence,
    )


def render_export_document(
    entries: List[Dict[str, Any]],
    fmt: str,
    *,
    include_transcript: bool = True,
    include_intelligence: bool = True,
) -> str:
    """Render all entries as one combined document."""
    if fmt == FORMAT_JSON:
        meetings = [
            json.loads(
                render_meeting_document(
                    entry,
                    fmt,
                    include_transcript=include_transcript,
                    include_intelligence=include_intelligence,
                )
            )
            for entry in entries
        ]
        envelope = {
            "format_version": FORMAT_VERSION,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "meetings": meetings,
        }
        return json.dumps(envelope, indent=2, ensure_ascii=False)
    if fmt == FORMAT_TXT:
        separator = "\n\n============================================================\n\n"
        return separator.join(
            render_meeting_document(
                entry,
                fmt,
                include_transcript=include_transcript,
                include_intelligence=include_intelligence,
            )
            for entry in entries
        )
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = (
        f"# Past Meetings Export\n\n"
        f"{len(entries)} meeting(s) · exported {stamp}\n\n"
        "---\n\n"
    )
    body = "\n\n---\n\n".join(
        render_meeting_document(
            entry,
            fmt,
            include_transcript=include_transcript,
            include_intelligence=include_intelligence,
        ).rstrip()
        for entry in entries
    )
    return header + body + "\n"


def write_per_meeting_files(
    entries: List[Dict[str, Any]],
    fmt: str,
    out_dir: str,
    *,
    include_transcript: bool = True,
    include_intelligence: bool = True,
) -> List[str]:
    """Write one file per entry, returning the paths written in order.

    Names collide only for same-second untitled meetings; a numeric suffix
    disambiguates within the chosen directory.
    """
    os.makedirs(out_dir, exist_ok=True)
    used: set[str] = set()
    written: List[str] = []
    for entry in entries:
        stem = meeting_file_stem(entry["meeting"], entry["state"])
        candidate = stem
        suffix = 2
        document = render_meeting_document(
            entry,
            fmt,
            include_transcript=include_transcript,
            include_intelligence=include_intelligence,
        )
        while True:
            filename = f"{candidate}.{fmt}"
            path = os.path.join(out_dir, filename)
            if filename in used:
                candidate = f"{stem}-{suffix}"
                suffix += 1
                continue
            try:
                with open(path, "x", encoding="utf-8") as handle:
                    handle.write(document)
            except FileExistsError:
                candidate = f"{stem}-{suffix}"
                suffix += 1
                continue
            break
        used.add(filename)
        written.append(path)
    return written


def meeting_file_stem(meeting: Dict[str, Any], state: Dict[str, Any]) -> str:
    """Filesystem-safe ``YYYYMMDD_HHMMSS_title`` stem for one meeting."""
    started = as_local_time(meeting.get("started_at"))
    stamp = started.strftime("%Y%m%d_%H%M%S") if started else "unknown_date"
    title = resolve_title(meeting, state).strip()
    slug = re.sub(r"\s+", "-", title)
    slug = re.sub(r"[^\w\-.]", "", slug).strip("-._")
    slug = slug[:_FILE_STEM_MAX_TITLE].rstrip("-._")
    return f"{stamp}_{slug}" if slug else stamp


def _loads_or_none(raw: str) -> Optional[Any]:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
