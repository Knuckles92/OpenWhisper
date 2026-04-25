"""
SQLAlchemy ORM models for OpenWhisper database.
Defines persistent entities for transcription history.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Float, Index, Integer, String, Text,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column,
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
