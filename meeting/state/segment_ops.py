"""Inverse-op helper for segment-log store mutations.

``MeetingStateStore`` requires a ``segment_handler`` so
``reassign_segment_speaker`` and ``revise_segment_text`` can produce undo
ops. Persistence itself is write-through via ``repository.on_ops_applied``;
this helper only reads the prior row and returns the inverse.

Used by the live engine and by headless post-meeting passes (insight re-run,
cloud speaker identification) so they share one implementation.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from meeting.interfaces import OpResult

logger = logging.getLogger(__name__)

SegmentHandler = Callable[[OpResult], Optional[Dict[str, Any]]]


def handle_segment_op(
    repository: Any,
    meeting_id: str,
    result: OpResult,
) -> Optional[Dict[str, Any]]:
    """Build the inverse op for one validated segment mutation.

    Args:
        repository: A ``MeetingRepository`` that can ``get_segment``.
        meeting_id: Owning meeting id.
        result: A validated segment-log op result.

    Returns:
        The inverse op dict restoring the prior row fields, or None when
        the segment had no prior row.
    """
    effect = result.effect or {}
    op_name = (result.op or {}).get("op")
    segment_id = effect.get("segment_id")
    prior = None
    try:
        prior = repository.get_segment(meeting_id, segment_id)
    except Exception:
        logger.exception("Failed to read prior segment %s", segment_id)
    if prior is None:
        return None

    if op_name == "revise_segment_text":
        # Persistence is via on_ops_applied/_mirror_effect; enrich the
        # broadcast effect with the full post-apply segment shape.
        new_text = effect.get("text") or ""
        updated = dict(prior)
        updated["text"] = new_text
        result.effect = {
            "entity": "segment_text",
            "segment_id": segment_id,
            "text": new_text,
            "segment": updated,
        }
        return {
            "op": "revise_segment_text",
            "segment_id": segment_id,
            "text": prior.get("text") or "",
            "evidence": [segment_id],
        }

    return {
        "op": "reassign_segment_speaker",
        "segment_id": segment_id,
        "participant_id": prior.get("speaker_participant_id"),
        "_source": prior.get("speaker_source", "channel"),
        "_pinned": bool(prior.get("speaker_pinned")),
        "force": True,
    }


def make_segment_handler(repository: Any, meeting_id: str) -> SegmentHandler:
    """Return a store ``segment_handler`` bound to one meeting."""

    def _handler(result: OpResult) -> Optional[Dict[str, Any]]:
        return handle_segment_op(repository, meeting_id, result)

    return _handler
