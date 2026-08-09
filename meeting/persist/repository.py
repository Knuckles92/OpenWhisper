"""SQL repository for Meeting Mode: every meeting_* statement behind one seam.

Uses the app-wide ``DatabaseManager`` (SQLite + WAL) via its ``get_session``
context manager. All public methods accept and return plain dicts /
``TranscriptSegment`` values so callers never hold detached ORM objects.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text as sql_text

from meeting.interfaces import OpResult, TranscriptSegment
from services.models import (
    MeetingAudioChunk,
    MeetingEvent,
    MeetingParticipant,
    MeetingQuestion,
    MeetingSegment,
    MeetingSession,
    MeetingStateItem,
)

logger = logging.getLogger(__name__)

#: Failed chunks are retried until this many attempts.
MAX_CHUNK_ATTEMPTS = 3


def _now_iso() -> str:
    return datetime.now().isoformat()


def _session_to_dict(row: MeetingSession) -> Dict[str, Any]:
    return {
        "id": row.id, "title": row.title, "status": row.status,
        "started_at": row.started_at, "ended_at": row.ended_at,
        "paused_total_s": row.paused_total_s,
        "host_token": row.host_token, "guest_token": row.guest_token,
        "cloud_enabled": row.cloud_enabled,
        "asr_model": row.asr_model,
        "agent_provider": row.agent_provider, "agent_model": row.agent_model,
        "spool_dir": row.spool_dir,
        "state_json": row.state_json, "state_seq": row.state_seq,
        "app_pid": row.app_pid, "app_heartbeat_at": row.app_heartbeat_at,
    }


def _chunk_to_dict(row: MeetingAudioChunk) -> Dict[str, Any]:
    return {
        "id": row.id, "meeting_id": row.meeting_id, "channel": row.channel,
        "seq": row.seq, "file_path": row.file_path,
        "start_s": row.start_s, "duration_s": row.duration_s,
        "sample_rate": row.sample_rate, "asr_status": row.asr_status,
        "asr_attempts": row.asr_attempts, "asr_error": row.asr_error,
    }


def _segment_to_dict(row: MeetingSegment) -> Dict[str, Any]:
    return {
        "id": row.id, "meeting_id": row.meeting_id, "chunk_id": row.chunk_id,
        "channel": row.channel, "start_s": row.start_s, "end_s": row.end_s,
        "text": row.text,
        "speaker_participant_id": row.speaker_participant_id,
        "speaker_source": row.speaker_source,
        "speaker_pinned": row.speaker_pinned,
        "created_at": row.created_at,
    }


class SqlMeetingRepository:
    """Implements ``meeting.interfaces.MeetingRepository`` on the app database."""

    def __init__(self, db: Optional[Any] = None) -> None:
        """Args:
            db: A ``DatabaseManager``-compatible object (``get_session``
                context manager + ``engine``). Defaults to the app singleton;
                injectable for tests.
        """
        if db is None:
            from services.database import db as app_db
            db = app_db
        self._db = db

    # ------------------------------------------------------------------
    # Meetings
    # ------------------------------------------------------------------

    def create_meeting(self, **fields) -> None:
        with self._db.get_session() as session:
            session.add(MeetingSession(**fields))

    def update_meeting(self, meeting_id: str, **fields) -> None:
        with self._db.get_session() as session:
            row = session.get(MeetingSession, meeting_id)
            if row is None:
                logger.warning("update_meeting: unknown meeting %s", meeting_id)
                return
            for key, value in fields.items():
                setattr(row, key, value)

    def rename_meeting(self, meeting_id: str, title: str) -> None:
        """Update the canonical title and persisted state in one transaction."""
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("title required")
        with self._db.get_session() as session:
            row = session.get(MeetingSession, meeting_id)
            if row is None:
                raise ValueError(f"unknown meeting '{meeting_id}'")
            state: Dict[str, Any] = {}
            if row.state_json:
                try:
                    state = json.loads(row.state_json)
                except (TypeError, ValueError):
                    logger.warning("Replacing corrupt state for meeting %s", meeting_id)
            state["meeting_id"] = meeting_id
            state["title"] = clean_title
            row.title = clean_title
            row.state_json = json.dumps(state, ensure_ascii=False)

    def replace_tokens(self, meeting_id: str, host_token: str,
                       guest_token: str) -> None:
        """Persist a freshly generated capability-token pair."""
        with self._db.get_session() as session:
            row = session.get(MeetingSession, meeting_id)
            if row is None:
                raise ValueError(f"unknown meeting '{meeting_id}'")
            row.host_token = host_token
            row.guest_token = guest_token

    def get_meeting(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        with self._db.get_session() as session:
            row = session.get(MeetingSession, meeting_id)
            return _session_to_dict(row) if row else None

    def list_meetings(self) -> List[Dict[str, Any]]:
        with self._db.get_session() as session:
            rows = session.query(MeetingSession).order_by(
                MeetingSession.started_at.desc()
            ).all()
            return [_session_to_dict(r) for r in rows]

    def delete_meeting(self, meeting_id: str) -> None:
        """Delete a meeting and all child rows.

        Children are deleted explicitly (not via FK cascade) because SQLite
        does not fire the FTS sync triggers for cascade deletes, which would
        orphan transcript text in the search index.
        """
        with self._db.get_session() as session:
            for model in (MeetingEvent, MeetingQuestion, MeetingStateItem,
                          MeetingParticipant, MeetingSegment, MeetingAudioChunk):
                session.query(model).filter(
                    model.meeting_id == meeting_id
                ).delete(synchronize_session=False)
            row = session.get(MeetingSession, meeting_id)
            if row is not None:
                session.delete(row)

    def heartbeat(self, meeting_id: str) -> None:
        self.update_meeting(
            meeting_id, app_pid=os.getpid(), app_heartbeat_at=_now_iso()
        )

    def find_interrupted_meetings(self) -> List[Dict[str, Any]]:
        """Return live/recovery sessions and terminal sessions with pending ASR."""
        with self._db.get_session() as session:
            unfinished = session.query(MeetingAudioChunk.meeting_id).filter(
                MeetingAudioChunk.asr_status != "done"
            )
            rows = session.query(MeetingSession).filter(
                (MeetingSession.status.in_(("active", "paused", "needs_recovery")))
                | (MeetingSession.id.in_(unfinished))
            ).all()
            return [_session_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Audio chunks
    # ------------------------------------------------------------------

    def register_chunk(self, **fields) -> int:
        with self._db.get_session() as session:
            row = MeetingAudioChunk(**fields)
            session.add(row)
            session.flush()  # assign autoincrement id before commit
            return row.id

    def set_chunk_status(self, chunk_id: int, status: str,
                         error: Optional[str] = None) -> None:
        with self._db.get_session() as session:
            row = session.get(MeetingAudioChunk, chunk_id)
            if row is None:
                return
            if row.asr_status == "done" and status != "done":
                return
            row.asr_status = status
            if status == "processing":
                row.asr_attempts += 1
            if error is not None:
                row.asr_error = error[:2000]

    def get_pending_chunks(self, meeting_id: str) -> List[Dict[str, Any]]:
        """Chunks awaiting transcription, including retryable failures.

        ``processing`` counts as retryable: a chunk in that state belongs to
        a transcription that never reported back (a crash mid-Whisper), and
        excluding it would silently lose 20-32 s of speech forever. Attempts
        are incremented on the transition into ``processing``, so the retry
        budget stays bounded.
        """
        with self._db.get_session() as session:
            rows = session.query(MeetingAudioChunk).filter(
                MeetingAudioChunk.meeting_id == meeting_id,
                (
                    (MeetingAudioChunk.asr_status == "pending")
                    | (
                        MeetingAudioChunk.asr_status.in_(
                            ("failed", "processing")
                        )
                        & (MeetingAudioChunk.asr_attempts < MAX_CHUNK_ATTEMPTS)
                    )
                ),
            ).order_by(MeetingAudioChunk.start_s).all()
            return [_chunk_to_dict(r) for r in rows]

    def count_unfinished_chunks(self, meeting_id: str) -> int:
        """Count every chunk that has not reached a durable done state."""
        with self._db.get_session() as session:
            return session.query(MeetingAudioChunk).filter(
                MeetingAudioChunk.meeting_id == meeting_id,
                MeetingAudioChunk.asr_status != "done",
            ).count()

    def reset_unfinished_chunks(self, meeting_id: str) -> int:
        """Reset all unfinished chunks for an explicit recovery attempt."""
        with self._db.get_session() as session:
            rows = session.query(MeetingAudioChunk).filter(
                MeetingAudioChunk.meeting_id == meeting_id,
                MeetingAudioChunk.asr_status != "done",
            ).all()
            for row in rows:
                row.asr_status = "pending"
                row.asr_attempts = 0
                row.asr_error = None
            return len(rows)

    def get_audio_chunks(self, meeting_id: str) -> List[Dict[str, Any]]:
        """Return durable audio chunks in timeline/channel order."""
        with self._db.get_session() as session:
            rows = session.query(MeetingAudioChunk).filter(
                MeetingAudioChunk.meeting_id == meeting_id
            ).order_by(
                MeetingAudioChunk.start_s,
                MeetingAudioChunk.channel,
                MeetingAudioChunk.seq,
            ).all()
            return [_chunk_to_dict(row) for row in rows]

    def next_chunk_seq(self, meeting_id: str, channel: str) -> int:
        """Return the next unused sequence for a restarted capture channel."""
        with self._db.get_session() as session:
            row = session.query(MeetingAudioChunk.seq).filter(
                MeetingAudioChunk.meeting_id == meeting_id,
                MeetingAudioChunk.channel == channel,
            ).order_by(MeetingAudioChunk.seq.desc()).first()
            return int(row[0]) + 1 if row is not None else 0

    # ------------------------------------------------------------------
    # Segments
    # ------------------------------------------------------------------

    def add_segments(self, segments: List[TranscriptSegment]) -> None:
        if not segments:
            return
        created = _now_iso()
        with self._db.get_session() as session:
            for seg in segments:
                session.add(MeetingSegment(
                    id=seg.segment_id,
                    meeting_id=seg.meeting_id,
                    chunk_id=seg.chunk_id,
                    channel=seg.channel,
                    start_s=seg.start_s,
                    end_s=seg.end_s,
                    text=seg.text,
                    speaker_participant_id=seg.speaker_participant_id,
                    speaker_source=seg.speaker_source,
                    speaker_pinned=seg.speaker_pinned,
                    embedding=seg.embedding,
                    created_at=created,
                ))

    def commit_chunk_transcription(
        self,
        meeting_id: str,
        chunk_id: int,
        segments: List[TranscriptSegment],
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Atomically persist a chunk result and mark its ASR work complete.

        Returns:
            Canonical stored segment rows and whether this call committed them.
        """
        created = _now_iso()
        with self._db.get_session() as session:
            chunk = session.query(MeetingAudioChunk).filter(
                MeetingAudioChunk.id == chunk_id,
                MeetingAudioChunk.meeting_id == meeting_id,
            ).one_or_none()
            if chunk is None:
                raise ValueError("unknown meeting audio chunk")
            if chunk.asr_status == "done":
                rows = session.query(MeetingSegment).filter(
                    MeetingSegment.meeting_id == meeting_id,
                    MeetingSegment.chunk_id == chunk_id,
                ).order_by(MeetingSegment.start_s, MeetingSegment.id).all()
                return [_segment_to_dict(row) for row in rows], False
            for seg in segments:
                if seg.meeting_id != meeting_id or seg.chunk_id != chunk_id:
                    raise ValueError("segment does not belong to chunk")
                existing = session.get(MeetingSegment, seg.segment_id)
                if existing is not None and (
                    existing.meeting_id != meeting_id
                    or existing.chunk_id != chunk_id
                ):
                    raise ValueError("segment id already belongs to another chunk")
                session.merge(MeetingSegment(
                    id=seg.segment_id,
                    meeting_id=seg.meeting_id,
                    chunk_id=seg.chunk_id,
                    channel=seg.channel,
                    start_s=seg.start_s,
                    end_s=seg.end_s,
                    text=seg.text,
                    speaker_participant_id=seg.speaker_participant_id,
                    speaker_source=seg.speaker_source,
                    speaker_pinned=seg.speaker_pinned,
                    embedding=seg.embedding,
                    created_at=created,
                ))
            chunk.asr_status = "done"
            chunk.asr_error = None
            session.flush()
            rows = session.query(MeetingSegment).filter(
                MeetingSegment.meeting_id == meeting_id,
                MeetingSegment.chunk_id == chunk_id,
            ).order_by(MeetingSegment.start_s, MeetingSegment.id).all()
            return [_segment_to_dict(row) for row in rows], True

    def get_segment(self, meeting_id: str, segment_id: str) -> Optional[Dict[str, Any]]:
        with self._db.get_session() as session:
            row = session.query(MeetingSegment).filter(
                MeetingSegment.meeting_id == meeting_id,
                MeetingSegment.id == segment_id,
            ).one_or_none()
            return _segment_to_dict(row) if row else None

    def segment_exists(self, meeting_id: str, segment_id: str) -> bool:
        with self._db.get_session() as session:
            return session.query(MeetingSegment.id).filter(
                MeetingSegment.meeting_id == meeting_id,
                MeetingSegment.id == segment_id
            ).first() is not None

    def update_segment_speaker(self, meeting_id: str, segment_id: str,
                               participant_id: Optional[str],
                               source: str, pinned: bool) -> None:
        with self._db.get_session() as session:
            row = session.query(MeetingSegment).filter(
                MeetingSegment.meeting_id == meeting_id,
                MeetingSegment.id == segment_id,
            ).one_or_none()
            if row is None:
                return
            if participant_id is not None:
                participant = session.query(MeetingParticipant.id).filter(
                    MeetingParticipant.meeting_id == meeting_id,
                    MeetingParticipant.id == participant_id,
                ).first()
                if participant is None:
                    raise ValueError("participant does not belong to meeting")
            row.speaker_participant_id = participant_id
            row.speaker_source = source
            row.speaker_pinned = pinned

    def get_segments(self, meeting_id: str, after_start_s: float = -1.0,
                     limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._db.get_session() as session:
            q = session.query(MeetingSegment).filter(
                MeetingSegment.meeting_id == meeting_id,
                MeetingSegment.start_s > after_start_s,
            ).order_by(MeetingSegment.start_s)
            if limit:
                q = q.limit(limit)
            return [_segment_to_dict(r) for r in q.all()]

    def get_segments_page(
        self,
        meeting_id: str,
        cursor_start_s: Optional[float] = None,
        cursor_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Return one deterministic keyset page ordered by time and id."""
        limit = max(1, min(int(limit), 1001))
        with self._db.get_session() as session:
            q = session.query(MeetingSegment).filter(
                MeetingSegment.meeting_id == meeting_id
            )
            if cursor_start_s is not None and cursor_id is not None:
                q = q.filter(
                    (MeetingSegment.start_s > float(cursor_start_s))
                    | (
                        (MeetingSegment.start_s == float(cursor_start_s))
                        & (MeetingSegment.id > cursor_id)
                    )
                )
            rows = q.order_by(
                MeetingSegment.start_s, MeetingSegment.id
            ).limit(limit).all()
            return [_segment_to_dict(row) for row in rows]

    def get_last_segments(self, meeting_id: str, count: int) -> List[Dict[str, Any]]:
        """The most recent ``count`` segments in chronological order."""
        with self._db.get_session() as session:
            rows = session.query(MeetingSegment).filter(
                MeetingSegment.meeting_id == meeting_id
            ).order_by(MeetingSegment.start_s.desc()).limit(count).all()
            return [_segment_to_dict(r) for r in reversed(rows)]

    def set_segment_embedding(self, meeting_id: str, segment_id: str,
                              embedding: bytes) -> None:
        with self._db.get_session() as session:
            row = session.query(MeetingSegment).filter(
                MeetingSegment.meeting_id == meeting_id,
                MeetingSegment.id == segment_id,
            ).one_or_none()
            if row is not None:
                row.embedding = embedding

    def get_segment_embeddings(self, meeting_id: str) -> List[Dict[str, Any]]:
        """Segments of a meeting that carry embeddings (for re-clustering)."""
        with self._db.get_session() as session:
            rows = session.query(MeetingSegment).filter(
                MeetingSegment.meeting_id == meeting_id,
                MeetingSegment.embedding.isnot(None),
            ).order_by(MeetingSegment.start_s).all()
            return [
                {**_segment_to_dict(r), "embedding": r.embedding} for r in rows
            ]

    # ------------------------------------------------------------------
    # State write-through + audit
    # ------------------------------------------------------------------

    def on_ops_applied(self, meeting_id: str, state: Dict[str, Any],
                       results: List[OpResult], actor_type: str,
                       actor_id: Optional[str]) -> None:
        """Persist applied ops: audit events, entity mirrors, state snapshot."""
        ts = _now_iso()
        with self._db.get_session() as session:
            for result in results:
                undo_seq = result.op.get("_undo_event_seq")
                action = result.op.get("op", "unknown")
                if actor_type == "system" and isinstance(undo_seq, int):
                    action = f"undo:{undo_seq}"
                session.add(MeetingEvent(
                    meeting_id=meeting_id,
                    seq=result.seq or 0,
                    ts=ts,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action=action,
                    target_id=result.target_id,
                    payload_json=json.dumps(result.op, ensure_ascii=False),
                    inverse_json=json.dumps(result.inverse, ensure_ascii=False)
                    if result.inverse else None,
                ))
                self._mirror_effect(session, meeting_id, result)

            row = session.get(MeetingSession, meeting_id)
            if row is not None:
                row.state_json = json.dumps(state, ensure_ascii=False)
                row.state_seq = int(state.get("seq", 0))
                row.title = state.get("title", row.title)
                row.cloud_enabled = bool(state.get("cloud_enabled", row.cloud_enabled))

    def persist_state(self, meeting_id: str, state: Dict[str, Any]) -> None:
        """Persist a non-audited lifecycle/status snapshot atomically."""
        with self._db.get_session() as session:
            row = session.get(MeetingSession, meeting_id)
            if row is None:
                raise ValueError(f"unknown meeting '{meeting_id}'")
            row.state_json = json.dumps(state, ensure_ascii=False)
            row.state_seq = int(state.get("seq", row.state_seq or 0))
            row.status = state.get("status", row.status)
            row.title = state.get("title", row.title)
            row.cloud_enabled = bool(state.get("cloud_enabled", row.cloud_enabled))

    def _mirror_effect(self, session, meeting_id: str, result: OpResult) -> None:
        """Write-through mirror of one applied op's effect."""
        effect = result.effect or {}
        entity = effect.get("entity")
        if entity == "item":
            item = effect["item"]
            session.merge(MeetingStateItem(
                id=item["id"], meeting_id=meeting_id, card=item["card"],
                text=item["text"],
                data_json=json.dumps(item.get("data") or {}, ensure_ascii=False),
                status=item["status"], author_type=item["author_type"],
                author_id=item.get("author_id"), pinned=item["pinned"],
                revision=item["revision"],
                evidence_json=json.dumps(item.get("evidence") or []),
                created_at=item["created_at"], updated_at=item["updated_at"],
            ))
        elif entity == "question":
            q = effect["question"]
            session.merge(MeetingQuestion(
                id=q["id"], meeting_id=meeting_id, text=q["text"],
                status=q["status"], answer_text=q.get("answer"),
                answer_source=q.get("answer_source"),
                answer_confidence=q.get("confidence"),
                suggested_answer_text=q.get("suggested_answer"),
                suggested_confidence=q.get("suggested_confidence"),
                thread_json=json.dumps(q.get("thread") or [], ensure_ascii=False),
                evidence_json=json.dumps(q.get("evidence") or []),
                asked_at=q["asked_at"], resolved_at=q.get("resolved_at"),
                resolved_by=q.get("resolved_by"),
            ))
        elif entity == "participant":
            p = effect["participant"]
            session.merge(MeetingParticipant(
                id=p["id"], meeting_id=meeting_id,
                display_name=p["display_name"], kind=p["kind"],
                name_source=p["name_source"],
                is_provisional=p["is_provisional"],
                created_at=p["created_at"], updated_at=p["updated_at"],
            ))
        elif entity == "segment_speaker":
            row = session.query(MeetingSegment).filter(
                MeetingSegment.meeting_id == meeting_id,
                MeetingSegment.id == effect["segment_id"],
            ).one_or_none()
            if row is None:
                raise ValueError("segment does not belong to meeting")
            participant_id = effect.get("participant_id")
            if participant_id is not None:
                participant = session.query(MeetingParticipant.id).filter(
                    MeetingParticipant.meeting_id == meeting_id,
                    MeetingParticipant.id == participant_id,
                ).first()
                if participant is None:
                    raise ValueError("participant does not belong to meeting")
            row.speaker_participant_id = participant_id
            row.speaker_source = effect.get("source", "human")
            row.speaker_pinned = bool(effect.get("pinned"))

    def get_event(self, meeting_id: str, seq: int) -> Optional[Dict[str, Any]]:
        with self._db.get_session() as session:
            row = session.query(MeetingEvent).filter(
                MeetingEvent.meeting_id == meeting_id,
                MeetingEvent.seq == seq,
            ).first()
            if row is None:
                return None
            return {
                "seq": row.seq, "ts": row.ts,
                "actor_type": row.actor_type, "actor_id": row.actor_id,
                "action": row.action, "target_id": row.target_id,
                "payload": json.loads(row.payload_json),
                "inverse": json.loads(row.inverse_json)
                if row.inverse_json else None,
            }

    def list_events(self, meeting_id: str, before_seq: Optional[int] = None,
                    limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent audit events with undo availability."""
        limit = max(1, min(int(limit), 500))
        with self._db.get_session() as session:
            undo_actions = {
                action for (action,) in session.query(MeetingEvent.action).filter(
                    MeetingEvent.meeting_id == meeting_id,
                    MeetingEvent.action.like("undo:%"),
                ).all()
            }
            q = session.query(MeetingEvent).filter(
                MeetingEvent.meeting_id == meeting_id
            )
            if before_seq is not None:
                q = q.filter(MeetingEvent.seq < int(before_seq))
            rows = q.order_by(MeetingEvent.seq.desc()).limit(limit).all()
            return [{
                "seq": row.seq,
                "ts": row.ts,
                "actor_type": row.actor_type,
                "actor_id": row.actor_id,
                "action": row.action,
                "target_id": row.target_id,
                "undoable": (
                    bool(row.inverse_json)
                    and f"undo:{row.seq}" not in undo_actions
                ),
            } for row in rows]

    def event_is_undone(self, meeting_id: str, seq: int) -> bool:
        """Whether a successful audit event already reverted ``seq``."""
        with self._db.get_session() as session:
            return session.query(MeetingEvent.seq).filter(
                MeetingEvent.meeting_id == meeting_id,
                MeetingEvent.action == f"undo:{int(seq)}",
            ).first() is not None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_transcripts(self, query: str) -> List[Dict[str, Any]]:
        """Full-text search across all meeting transcripts.

        Returns segment matches with meeting metadata, newest meetings first.
        Degrades to an empty list when the FTS index is unavailable.
        """
        query = (query or "").strip()
        if not query:
            return []
        # Quote each term to keep FTS5 operators from leaking in from user input.
        match_expr = " ".join(
            '"{}"'.format(term.replace('"', '""')) for term in query.split()
        )
        stmt = sql_text("""
            SELECT ms.id AS segment_id, ms.meeting_id, ms.start_s, ms.end_s,
                   ms.text, s.title, s.started_at,
                   snippet(meeting_segments_fts, 0, '[', ']', ' … ', 12) AS snippet
            FROM meeting_segments_fts
            JOIN meeting_segments ms ON ms.rowid = meeting_segments_fts.rowid
            JOIN meeting_sessions s ON s.id = ms.meeting_id
            WHERE meeting_segments_fts MATCH :q
            ORDER BY s.started_at DESC, ms.start_s
            LIMIT 200
        """)
        try:
            with self._db.engine.connect() as conn:
                rows = conn.execute(stmt, {"q": match_expr}).mappings().all()
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("Meeting transcript search failed")
            return []
