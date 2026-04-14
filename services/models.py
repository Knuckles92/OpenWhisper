"""
SQLAlchemy ORM models for OpenWhisper database.
Defines all persistent entities: transcription history, meetings, and chunks.
"""
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Float, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship,
)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all models."""
    pass


# ---------------------------------------------------------------------------
# Schema version tracking
# ---------------------------------------------------------------------------

class SchemaVersion(Base):
    __tablename__ = 'schema_version'

    version: Mapped[int] = mapped_column(Integer, primary_key=True)


# ---------------------------------------------------------------------------
# Transcription history
# ---------------------------------------------------------------------------

class TranscriptionHistory(Base):
    """A single transcription history entry (replaces HistoryEntry dataclass)."""
    __tablename__ = 'transcription_history'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    audio_file: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    transcription_time: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    audio_duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index('idx_history_timestamp', 'timestamp'),
    )

    # -- Factory ----------------------------------------------------------

    @classmethod
    def create(
        cls,
        text: str,
        model: str,
        audio_file: Optional[str] = None,
        transcription_time: Optional[float] = None,
        audio_duration: Optional[float] = None,
        file_size: Optional[int] = None,
    ) -> 'TranscriptionHistory':
        """Create a new entry with auto-generated id and timestamp."""
        return cls(
            id=str(uuid.uuid4()),
            text=text,
            timestamp=datetime.now().isoformat(),
            model=model,
            audio_file=audio_file,
            transcription_time=transcription_time,
            audio_duration=audio_duration,
            file_size=file_size,
        )

    # -- Display helpers (ported from HistoryEntry dataclass) -------------

    @property
    def formatted_timestamp(self) -> str:
        try:
            dt = datetime.fromisoformat(self.timestamp)
            return dt.strftime("%b %d, %Y %I:%M %p")
        except Exception:
            return self.timestamp

    @property
    def preview_text(self) -> str:
        max_len = 100
        if len(self.text) <= max_len:
            return self.text
        return self.text[:max_len].rsplit(' ', 1)[0] + "..."


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------

class Meeting(Base):
    """A complete meeting record (replaces MeetingEntry dataclass)."""
    __tablename__ = 'meetings'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    start_time: Mapped[str] = mapped_column(String, nullable=False)
    end_time: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    transcript: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String, default='in_progress')
    audio_file: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Relationships
    chunks: Mapped[List['MeetingChunk']] = relationship(
        back_populates='meeting',
        cascade='all, delete-orphan',
        order_by='MeetingChunk.chunk_index',
        passive_deletes=True,
    )

    __table_args__ = (
        Index('idx_meetings_start_time', 'start_time'),
    )

    # -- Factory ----------------------------------------------------------

    @classmethod
    def create(cls, title: str = '') -> 'Meeting':
        """Create a new meeting with auto-generated id and start time."""
        return cls(
            id=str(uuid.uuid4()),
            title=title or f"Meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            start_time=datetime.now().isoformat(),
            end_time=None,
            duration_seconds=0.0,
            transcript='',
            status='in_progress',
            audio_file=None,
        )

    # -- Display helpers (ported from MeetingEntry dataclass) -------------

    @property
    def formatted_start_time(self) -> str:
        try:
            dt = datetime.fromisoformat(self.start_time)
            return dt.strftime("%b %d, %Y %I:%M %p")
        except Exception:
            return self.start_time

    @property
    def formatted_duration(self) -> str:
        seconds = int(self.duration_seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{secs}s"

    @property
    def preview_text(self) -> str:
        max_len = 100
        if len(self.transcript) <= max_len:
            return self.transcript
        return self.transcript[:max_len].rsplit(' ', 1)[0] + "..."

    @property
    def audio_files(self) -> List[str]:
        """Computed from chunks — replaces the old dataclass field."""
        return [c.audio_file for c in self.chunks if c.audio_file]

    # -- Domain methods ---------------------------------------------------

    def add_chunk(self, text: str, audio_file: Optional[str] = None):
        """Add a transcription chunk to the meeting."""
        chunk = MeetingChunk(
            meeting_id=self.id,
            chunk_index=len(self.chunks),
            text=text,
            timestamp=datetime.now().isoformat(),
            audio_file=audio_file,
        )
        self.chunks.append(chunk)
        if self.transcript:
            self.transcript += ' ' + text
        else:
            self.transcript = text

    def complete(self, duration_seconds: float):
        """Mark the meeting as completed."""
        self.end_time = datetime.now().isoformat()
        self.duration_seconds = duration_seconds
        self.status = 'completed'

    def mark_interrupted(self, duration_seconds: float):
        """Mark the meeting as interrupted (e.g., crash recovery)."""
        self.end_time = datetime.now().isoformat()
        self.duration_seconds = duration_seconds
        self.status = 'interrupted'


# ---------------------------------------------------------------------------
# Meeting chunks
# ---------------------------------------------------------------------------

class MeetingChunk(Base):
    """A single transcription chunk belonging to a meeting."""
    __tablename__ = 'meeting_chunks'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        String, ForeignKey('meetings.id', ondelete='CASCADE'), nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)
    audio_file: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default='transcribed')
    start_offset_sec: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    end_offset_sec: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    meeting: Mapped['Meeting'] = relationship(back_populates='chunks')

    __table_args__ = (
        Index('idx_chunks_meeting_id', 'meeting_id'),
    )
