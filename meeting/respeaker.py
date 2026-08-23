"""Headless re-run of post-meeting OpenAI speaker identification.

A finished meeting can be relabeled after someone renames a speaker: the
new name becomes a reference clip on the next pass. No ``MeetingEngine``
is required — this rebuilds the stored state with a segment handler so
``reassign_segment_speaker`` persists and broadcasts like a live meeting.

No Qt imports; this package stays standalone-extractable.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from meeting.diarize.cloud_pass import DEFAULT_MODEL, run_cloud_speaker_pass
from meeting.reinsight import _load_state
from meeting.state.segment_ops import make_segment_handler
from meeting.state.store import MeetingStateStore

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str, int, int], None]
TranscribeFn = Callable[..., Any]

__all__ = ["rerun_speakers"]


def _open_store(repository: Any, meeting_id: str,
                meeting: Dict[str, Any]) -> MeetingStateStore:
    return MeetingStateStore(
        _load_state(meeting, meeting_id),
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


def rerun_speakers(
    repository: Any,
    meeting_id: str,
    *,
    api_key: str,
    store: Optional[MeetingStateStore] = None,
    spool_dir: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    transcribe_fn: Optional[TranscribeFn] = None,
    progress_cb: Optional[ProgressCb] = None,
) -> Dict[str, Any]:
    """Relabel speakers on a stored meeting from its loopback recording.

    Args:
        repository: A ``MeetingRepository``.
        meeting_id: The meeting to relabel.
        api_key: OpenAI API key (ignored when ``transcribe_fn`` is set).
        store: Optional existing ``MeetingStateStore``.
        spool_dir: Override for the meeting's stored spool directory.
        model: Transcription model id.
        transcribe_fn: Injectable decoder (tests).
        progress_cb: Optional progress callback.

    Returns:
        ``{ok, state, applied, created, error}``. Failures are reported
        here, not raised, except unknown-meeting ``ValueError``.

    Raises:
        ValueError: When the meeting is unknown.
    """
    meeting = repository.get_meeting(meeting_id)
    if meeting is None:
        raise ValueError("unknown meeting")
    if store is None:
        store = _open_store(repository, meeting_id, meeting)
    resolved_spool = spool_dir or meeting.get("spool_dir") or ""
    result = run_cloud_speaker_pass(
        repository, meeting_id, store, resolved_spool,
        api_key=api_key, model=model,
        transcribe_fn=transcribe_fn, progress_cb=progress_cb,
    )
    logger.info(
        "Speaker re-run for meeting %s finished: ok=%s applied=%s",
        meeting_id, result.get("ok"), result.get("applied"),
    )
    return {
        "ok": bool(result.get("ok")),
        "state": store.snapshot(),
        "applied": int(result.get("applied") or 0),
        "created": int(result.get("created") or 0),
        "windows": int(result.get("windows") or 0),
        "error": result.get("error"),
    }
