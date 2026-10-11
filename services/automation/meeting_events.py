"""Meeting engine events as public ``meeting.*`` events.

MeetingRuntime calls ``forward`` on the Qt thread for every event of the
current meeting's engine, after its own handling. Only four things become
public, each built from named fields so nothing private leaks: never the
engine's ``_``-prefixed keys, dashboard links or guest tokens, capture or
finalization details.

- ``meeting.started``: the first ``status`` event that says the meeting is
  running. The engine sends it once capture, the dashboard and processing
  are up, so a start that fails earlier publishes nothing. Once per meeting.
- ``meeting.ended``: the engine's ``ended`` event, with its status.
- ``meeting.segments``: transcript lines as the engine commits them. The
  replacement transcript a re-decode writes after the meeting ends is not
  new lines; read it from the History API.
- ``meeting.voice_command``: a spoken command the note-taker applied.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Mapping

from services.automation import catalog
from services.automation.events import publish

logger = logging.getLogger(__name__)

#: Engine statuses that mean the meeting is capturing.
_RUNNING = frozenset({"active", "paused"})
#: Meetings already announced; a handful is plenty, one runs at a time.
_ANNOUNCED_LIMIT = 32

_announced: "OrderedDict[str, None]" = OrderedDict()
_lock = threading.Lock()


def _first_announcement(meeting_id: str) -> bool:
    with _lock:
        if meeting_id in _announced:
            return False
        _announced[meeting_id] = None
        while len(_announced) > _ANNOUNCED_LIMIT:
            _announced.popitem(last=False)
        return True


def _seconds(value: Any):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 3)


def _segment(row: Mapping[str, Any]) -> dict:
    """One committed transcript row as a ``meeting.segments`` item."""
    speaker = row.get("speaker") if isinstance(row.get("speaker"), str) else ""
    return {
        "segment_id": str(row.get("id") or row.get("segment_id") or ""),
        # The engine's rows name the speaker by participant id; the History
        # API's meeting insights list each participant's display name.
        "speaker": speaker or str(row.get("speaker_participant_id") or ""),
        "text": str(row.get("text") or ""),
        "start_s": _seconds(row.get("start_s")),
        "end_s": _seconds(row.get("end_s")),
    }


def _forward(kind: str, payload: Mapping[str, Any], meeting_id: str) -> None:
    meeting_id = meeting_id or str(payload.get("meeting_id") or "")
    if not meeting_id:
        return
    if kind == "status":
        if payload.get("status") in _RUNNING and _first_announcement(meeting_id):
            title = payload.get("title")
            publish(catalog.MEETING_STARTED, catalog.meeting_started(
                meeting_id, title.strip() if isinstance(title, str) else ""
            ))
    elif kind == "ended":
        status = payload.get("status")
        publish(catalog.MEETING_ENDED, catalog.meeting_ended(
            meeting_id, status if isinstance(status, str) and status else "ended"
        ))
    elif kind == "segments":
        if "removed_ids" in payload:
            return  # a re-decode replacing the transcript, not new lines
        items = payload.get("items")
        rows = [
            _segment(row) for row in (items if isinstance(items, list) else ())
            if isinstance(row, Mapping) and str(row.get("text") or "").strip()
        ]
        if rows:
            publish(catalog.MEETING_SEGMENTS, catalog.meeting_segments(meeting_id, rows))
    elif kind == "voice_command":
        command = payload.get("command")
        if isinstance(command, str) and command.strip():
            publish(catalog.MEETING_VOICE_COMMAND, catalog.meeting_voice_command(
                meeting_id, command.strip()
            ))


def forward(kind: str, payload: Mapping[str, Any], *, meeting_id: str = "") -> None:
    """Publish the public event for one routed engine event; Qt thread, never raises.

    Args:
        kind: The engine listener kind (status, segments, ended, voice_command…).
        payload: The engine's payload; may carry private ``_``-prefixed keys.
        meeting_id: The engine's meeting, for payloads that don't name it.
    """
    try:
        _forward(kind, payload if isinstance(payload, Mapping) else {}, str(meeting_id or ""))
    except Exception as exc:
        logger.debug("Could not publish meeting event %s: %s", kind, type(exc).__name__)
