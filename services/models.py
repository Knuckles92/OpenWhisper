"""SQLAlchemy persistence models."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, Float, ForeignKey, Index, Integer, LargeBinary, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column,
)

from services.format_utils import format_timestamp


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = 'schema_version'

    version: Mapped[int] = mapped_column(Integer, primary_key=True)

class TranscriptionHistory(Base):
    """A single transcription history entry (replaces HistoryEntry dataclass)."""
    __tablename__ = 'transcription_history'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    audio_file: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    transcription_time: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    audio_duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Set only when post-ASR cleanup ran successfully on this entry.
    cleanup_provider: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cleanup_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index('idx_history_timestamp', 'timestamp'),
    )

    @classmethod
    def create(
        cls,
        text: str,
        model: str,
        audio_file: Optional[str] = None,
        transcription_time: Optional[float] = None,
        audio_duration: Optional[float] = None,
        file_size: Optional[int] = None,
        raw_text: Optional[str] = None,
        cleanup_provider: Optional[str] = None,
        cleanup_model: Optional[str] = None,
    ) -> 'TranscriptionHistory':
        """Create a new entry with auto-generated id and timestamp."""
        return cls(
            id=str(uuid.uuid4()),
            text=text,
            raw_text=raw_text,
            timestamp=datetime.now().isoformat(),
            model=model,
            audio_file=audio_file,
            transcription_time=transcription_time,
            audio_duration=audio_duration,
            file_size=file_size,
            cleanup_provider=cleanup_provider,
            cleanup_model=cleanup_model,
        )

    @property
    def formatted_timestamp(self) -> str:
        return format_timestamp(self.timestamp)

    @property
    def preview_text(self) -> str:
        max_len = 100
        if len(self.text) <= max_len:
            return self.text
        return self.text[:max_len].rsplit(' ', 1)[0] + "..."


# Meeting Mode
#
# Table names are deliberately distinct from the legacy meetings /
# meeting_chunks / meeting_insights tables, which DatabaseManager still drops
# on startup for users migrating from older versions.

class MeetingSession(Base):
    """One meeting: lifecycle, access tokens, and the latest state snapshot."""
    __tablename__ = 'meeting_sessions'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False, default='')
    # active | paused | ended | failed | needs_recovery
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[str] = mapped_column(String, nullable=False)
    ended_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    paused_total_s: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    host_token: Mapped[str] = mapped_column(String, nullable=False)
    guest_token: Mapped[str] = mapped_column(String, nullable=False)
    cloud_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    asr_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    agent_provider: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    agent_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Non-secret OpenAI-compatible endpoint snapshot (JSON object).
    agent_endpoint_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    spool_dir: Mapped[str] = mapped_column(String, nullable=False)
    # Latest full MeetingState snapshot for fast reload / history view.
    state_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Crash detection: pid + heartbeat let startup recovery spot dead sessions.
    app_pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    app_heartbeat_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index('idx_msessions_started', 'started_at'),
    )


class MeetingAudioChunk(Base):
    """A spooled WAV chunk: the durable capture and per-chunk ASR retry unit."""
    __tablename__ = 'meeting_audio_chunks'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey('meeting_sessions.id', ondelete='CASCADE'), nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)  # mic | loopback
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    start_s: Mapped[float] = mapped_column(Float, nullable=False)
    duration_s: Mapped[float] = mapped_column(Float, nullable=False)
    sample_rate: Mapped[int] = mapped_column(Integer, nullable=False)
    # pending | processing | done | failed
    asr_status: Mapped[str] = mapped_column(String, nullable=False, default='pending')
    asr_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    asr_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint('meeting_id', 'channel', 'seq', name='uq_mchunks_seq'),
        Index('idx_mchunks_pending', 'meeting_id', 'asr_status'),
    )


class MeetingSegment(Base):
    """A timestamped transcript segment; its id is a stable evidence anchor."""
    __tablename__ = 'meeting_segments'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey('meeting_sessions.id', ondelete='CASCADE'), nullable=False)
    chunk_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey('meeting_audio_chunks.id'), nullable=True)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    start_s: Mapped[float] = mapped_column(Float, nullable=False)
    end_s: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_participant_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # channel | diarizer | human
    speaker_source: Mapped[str] = mapped_column(String, nullable=False, default='channel')
    speaker_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # float32 speaker-embedding bytes; lets re-clustering survive a restart.
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index('idx_msegments_time', 'meeting_id', 'start_s'),
    )


class MeetingParticipant(Base):
    """A person in the meeting (host, diarized remote cluster, or web guest)."""
    __tablename__ = 'meeting_participants'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey('meeting_sessions.id', ondelete='CASCADE'), nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # me | others_cluster | guest
    # default | human | agent_inferred — human names are never auto-overwritten.
    name_source: Mapped[str] = mapped_column(String, nullable=False, default='default')
    is_provisional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class MeetingStateItem(Base):
    """Write-through mirror of one dashboard card item."""
    __tablename__ = 'meeting_state_items'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey('meeting_sessions.id', ondelete='CASCADE'), nullable=False)
    card: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    data_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # proposed | edited | confirmed | removed
    status: Mapped[str] = mapped_column(String, nullable=False)
    author_type: Mapped[str] = mapped_column(String, nullable=False)
    author_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default='[]')
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index('idx_mitems_meeting', 'meeting_id'),
    )


class MeetingQuestion(Base):
    """Write-through mirror of one quiet-inbox question."""
    __tablename__ = 'meeting_questions'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey('meeting_sessions.id', ondelete='CASCADE'), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # open | resolved | dismissed
    answer_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    answer_source: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # user | audio
    answer_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    suggested_answer_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suggested_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    thread_json: Mapped[str] = mapped_column(Text, nullable=False, default='[]')
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default='[]')
    asked_at: Mapped[str] = mapped_column(String, nullable=False)
    resolved_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class MeetingEvent(Base):
    """Append-only audit trail; ``inverse_json`` powers host undo."""
    __tablename__ = 'meeting_events'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey('meeting_sessions.id', ondelete='CASCADE'), nullable=False)
    # == the state seq assigned when this op was applied.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[str] = mapped_column(String, nullable=False)
    actor_type: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    inverse_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index('idx_mevents_meeting', 'meeting_id', 'seq'),
    )
