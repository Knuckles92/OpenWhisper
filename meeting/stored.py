"""Qt-free reading of persisted meetings: the one way to load a stored row.

Repository meeting rows carry the state document as ``state_json`` text.
Readers go through these helpers (built on
:func:`meeting.state.schema.parse_state_json`), so a corrupt or missing
snapshot is handled the same way everywhere.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from meeting.state.schema import FinalizationState, MeetingState, parse_state_json
from meeting.state.segment_ops import make_segment_handler
from meeting.state.store import MeetingStateStore, repository_segment_lookup

logger = logging.getLogger(__name__)


def stored_state_dict(meeting: Optional[dict[str, Any]]) -> dict[str, Any]:
    """A meeting row's saved state document, or ``{}`` when there is none."""
    return parse_state_json((meeting or {}).get("state_json")) or {}


def stored_endpoint(meeting: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The row's raw ``agent_endpoint_json`` snapshot, or None."""
    raw = (meeting or {}).get("agent_endpoint_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def meeting_endpoint(meeting: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the stored non-secret endpoint snapshot, if any."""
    try:
        from services.text_llm import snapshot_from_meeting

        return snapshot_from_meeting(meeting).to_dict()
    except Exception:
        return stored_endpoint(meeting)


def finalization_from_meeting_row(meeting: dict[str, Any]) -> FinalizationState:
    """Normalize the insights payload stored on a repository meeting row.

    Args:
        meeting: Repository meeting dict, possibly including ``state_json``.

    Returns:
        A historical finalization value safe to show on list UIs.
    """
    data = stored_state_dict(meeting)
    return FinalizationState.normalize_historical(
        data.get("finalization"),
        cloud_enabled=bool(
            data.get("cloud_enabled", meeting.get("cloud_enabled"))
        ),
        meeting_status=str(meeting.get("status") or "ended"),
    )


def compact_finalization_list_fields(meeting: dict[str, Any]) -> dict[str, Any]:
    """Public list-row fields derived from a meeting's finalization snapshot.

    Args:
        meeting: Repository meeting dict.

    Returns:
        Compact fields safe to expose on meeting-list APIs. Does not include
        ``state_json`` or step details.
    """
    status = str(meeting.get("status") or "ended")
    fin = finalization_from_meeting_row(meeting)
    fields: dict[str, Any] = {
        "finalization_status": fin.status,
        "finalization_deferred": bool(fin.card_deferred),
    }
    pill = fin.history_pill(meeting_status=status)
    if pill:
        fields["insights_pill"] = pill[0]
        fields["insights_tone"] = pill[1]
    return fields


def _historical_payload(
    meeting: dict[str, Any], meeting_id: str, data: dict[str, Any],
) -> dict[str, Any]:
    """The saved document as a past meeting should be served after a restart."""
    payload = dict(data)
    payload["meeting_id"] = meeting_id
    payload["title"] = str(meeting.get("title") or payload.get("title") or "")
    payload["status"] = str(meeting.get("status") or payload.get("status") or "ended")
    payload["cloud_enabled"] = bool(meeting.get("cloud_enabled", False))
    payload.setdefault("seq", int(meeting.get("state_seq") or 0))
    payload["finalization"] = FinalizationState.normalize_historical(
        payload.get("finalization"),
        cloud_enabled=payload["cloud_enabled"],
        meeting_status=payload["status"],
    ).to_dict()
    review = payload.get("insight_review")
    if isinstance(review, dict) and review.get("status") == "running":
        payload["insight_review"] = dict(
            review, status="unavailable",
            message="Review was interrupted. Retry when ready.",
        )
    reports = payload.get("custom_reports")
    if isinstance(reports, list):
        # A report whose worker died with the process is not still being
        # written; showing it as running would also block every retry.
        payload["custom_reports"] = [
            dict(report, status="failed",
                 message="This report was interrupted. Ask for it again.")
            if isinstance(report, dict) and report.get("status") == "running"
            else report
            for report in reports
        ]
    return payload


def _fresh_state(meeting: dict[str, Any], meeting_id: str) -> MeetingState:
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


def load_state(
    meeting: dict[str, Any],
    meeting_id: Optional[str] = None,
    *,
    historical: bool = False,
) -> MeetingState:
    """Rebuild a meeting's saved state document.

    Args:
        meeting: Repository meeting row.
        meeting_id: The meeting's id; defaults to ``meeting["id"]``.
        historical: Serve a past meeting that no live worker owns (history
            dashboard, stored REST snapshots). The row's id, title, status
            and AI insights flag win over the snapshot's, and work a restart
            interrupted (final insights, an insight review, a tailored
            report) reads as stopped rather than still running.

    Returns:
        The restored state. A missing or unreadable snapshot yields a fresh
        state that keeps the row's title and AI insights flag.
    """
    meeting_id = str(meeting_id or meeting.get("id") or "")
    raw = meeting.get("state_json")
    data = parse_state_json(raw)
    if data is None and raw:
        logger.warning(
            "Unreadable state_json for meeting %s; starting from a fresh state",
            meeting_id,
        )
    if historical:
        try:
            return MeetingState.from_dict(
                _historical_payload(meeting, meeting_id, data or {})
            )
        except Exception:
            logger.exception(
                "Corrupt state_json for meeting %s; starting from a fresh state",
                meeting_id,
            )
            return MeetingState.from_dict(_historical_payload(meeting, meeting_id, {}))
    if data is not None:
        try:
            return MeetingState.from_dict(data)
        except Exception:
            logger.exception(
                "Corrupt state_json for meeting %s; starting from a fresh state",
                meeting_id,
            )
    return _fresh_state(meeting, meeting_id)


def open_store(
    repository: Any,
    meeting_id: str,
    meeting: dict[str, Any],
    *,
    historical: bool = False,
) -> MeetingStateStore:
    """A write-through store over a persisted meeting (see :func:`load_state`)."""
    return MeetingStateStore(
        load_state(meeting, meeting_id, historical=historical),
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
        segment_lookup=repository_segment_lookup(repository, meeting_id),
    )
