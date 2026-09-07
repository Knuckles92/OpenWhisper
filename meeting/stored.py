"""Qt-free reconstruction of persisted meetings for post-meeting work."""
from __future__ import annotations

import json
import logging
from typing import Any

from meeting.state.schema import MeetingState
from meeting.state.segment_ops import make_segment_handler
from meeting.state.store import MeetingStateStore

logger = logging.getLogger(__name__)


def meeting_endpoint(meeting: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the stored non-secret endpoint snapshot, if any."""
    try:
        from services.text_llm import snapshot_from_meeting

        return snapshot_from_meeting(meeting).to_dict()
    except Exception:
        raw = (meeting or {}).get("agent_endpoint_json")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except Exception:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None


def load_state(meeting: dict[str, Any], meeting_id: str) -> MeetingState:
    """Restore a snapshot, or preserve meeting metadata in a fresh state."""
    raw = meeting.get("state_json")
    if raw:
        try:
            return MeetingState.from_dict(json.loads(raw))
        except Exception:
            logger.exception(
                "Corrupt state_json for meeting %s; starting from a fresh state",
                meeting_id,
            )
    try:
        from services.settings import resolve_meeting_report_views
        report_views = list(resolve_meeting_report_views())
    except Exception:
        report_views = ["ribbon", "brief", "signal"]
    return MeetingState(
        meeting_id=meeting_id,
        title=meeting.get("title", ""),
        # Carried over so the store's write-through cannot flip the recorded
        # cloud flag on a meeting that simply never got a snapshot.
        cloud_enabled=bool(meeting.get("cloud_enabled")),
        report_views=report_views,
    )


def open_store(
    repository: Any, meeting_id: str, meeting: dict[str, Any],
) -> MeetingStateStore:
    return MeetingStateStore(
        load_state(meeting, meeting_id),
        repository=repository,
        segment_handler=make_segment_handler(repository, meeting_id),
        segment_exists=lambda segment_id: repository.segment_exists(
            meeting_id, segment_id
        ),
        segment_pinned=lambda segment_id: bool(
            (repository.get_segment(meeting_id, segment_id) or {}).get(
                "speaker_pinned"
            )
        ),
    )
