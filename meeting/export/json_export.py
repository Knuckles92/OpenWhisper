"""JSON export: the complete machine-readable meeting artifact.

Unlike the Markdown export this preserves everything — full state document
(including evidence segment ids), all segments, and the meeting row — except
volatile fields that must never leave the app: the capability tokens
(``host_token``/``guest_token``), process-liveness bookkeeping, and the
``state_json`` snapshot that would duplicate the exported state.

Pure functions, standard library only.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

#: Version stamp for the export envelope; bump on breaking shape changes.
FORMAT_VERSION = 1

#: Meeting-row fields stripped from exports. The tokens grant dashboard
#: access and MUST NOT leak; pid/heartbeat are meaningless outside the app;
#: state_json duplicates the exported ``state`` document.
_VOLATILE_MEETING_FIELDS = frozenset({
    "host_token",
    "guest_token",
    "app_pid",
    "app_heartbeat_at",
    "state_json",
})


def export_json(meeting: Dict[str, Any], state: Dict[str, Any],
                segments: List[Dict[str, Any]]) -> str:
    """Serialize the full meeting record as pretty-printed JSON.

    Args:
        meeting: Meeting row dict (``get_meeting`` shape); volatile fields
            are stripped before serialization.
        state: ``MeetingState.to_dict()`` document.
        segments: Segment row dicts ordered by ``start_s``.

    Returns:
        A JSON document string with the envelope
        ``{"format_version": 1, "meeting": ..., "state": ..., "segments": ...}``.
    """
    safe_meeting = {
        key: value for key, value in (meeting or {}).items()
        if key not in _VOLATILE_MEETING_FIELDS
    }
    return json.dumps(
        {
            "format_version": FORMAT_VERSION,
            "meeting": safe_meeting,
            "state": state,
            "segments": segments,
        },
        indent=2,
        ensure_ascii=False,
    )
