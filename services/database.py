"""
SQLite database manager for transcription history and meeting storage.
Provides unified data persistence via SQLAlchemy ORM with migration support.
"""
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import List, Optional

from sqlalchemy import create_engine, event, func, inspect, text
from sqlalchemy.orm import joinedload, scoped_session, sessionmaker

from config import config
from services.models import (
    Base, MeetingChunk, MeetingInsight, Meeting,
    SchemaVersion, TranscriptionHistory,
)

# Schema version for future migrations
SCHEMA_VERSION = 5


class DatabaseManager:
    """Manages SQLite database for transcription and meeting storage."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or getattr(config, 'DATABASE_FILE', 'openwhisper.db')

        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
        )

        # Enable foreign keys for every raw SQLite connection
        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self._session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False,
        )
        self.Session = scoped_session(self._session_factory)

        self._init_database()
        self._migrate_from_json()

        logging.info(f"DatabaseManager initialized: {self.db_path}")

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    @contextmanager
    def get_session(self):
        """Yield a thread-scoped session with auto commit/rollback."""
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            self.Session.remove()

    # ------------------------------------------------------------------
    # Schema initialisation & migrations
    # ------------------------------------------------------------------

    def _init_database(self) -> None:
        """Create tables (if new DB) and run migrations for existing DBs."""
        # For existing databases, run migrations BEFORE create_all so that
        # ALTER TABLE statements can add columns that models expect.
        self._maybe_run_migrations()

        Base.metadata.create_all(self.engine)

        # Ensure schema_version row exists
        with self.get_session() as session:
            version_row = session.get(SchemaVersion, SCHEMA_VERSION)
            if not version_row:
                # Clear any old version rows and set current
                session.query(SchemaVersion).delete()
                session.add(SchemaVersion(version=SCHEMA_VERSION))

        logging.info("Database schema initialized")

    def _maybe_run_migrations(self) -> None:
        """Check if the database already exists and needs migrations."""
        insp = inspect(self.engine)
        if not insp.has_table('schema_version'):
            return  # Fresh database — create_all will handle everything

        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT version FROM schema_version LIMIT 1")).fetchone()
            if row is None:
                return
            current_version = row[0]
            if current_version < SCHEMA_VERSION:
                self._run_migrations(conn, current_version)
                conn.commit()

    def _run_migrations(self, conn, from_version: int) -> None:
        """Run progressive migrations using raw SQL (standard for non-Alembic projects)."""
        logging.info(f"Running database migrations from v{from_version} to v{SCHEMA_VERSION}")

        if from_version < 2:
            try:
                conn.execute(text("ALTER TABLE meetings ADD COLUMN audio_file TEXT DEFAULT NULL"))
                logging.info("Migration v1->v2: Added audio_file column to meetings table")
            except Exception as e:
                if "duplicate column name" not in str(e).lower():
                    raise
                logging.warning("Migration v1->v2: audio_file column already exists")

        if from_version < 3:
            try:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS meeting_insights (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        meeting_id TEXT NOT NULL,
                        insight_type TEXT NOT NULL,
                        content TEXT NOT NULL,
                        custom_prompt TEXT,
                        generated_at TEXT NOT NULL,
                        provider TEXT,
                        model TEXT,
                        FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
                    )
                """))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_insights_meeting_id ON meeting_insights(meeting_id)"
                ))
                conn.execute(text("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_insights_unique
                    ON meeting_insights(meeting_id, insight_type, COALESCE(custom_prompt, ''))
                """))
                logging.info("Migration v2->v3: Added meeting_insights table")
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise
                logging.warning("Migration v2->v3: meeting_insights table already exists")

        if from_version < 4:
            try:
                result = conn.execute(text("PRAGMA table_info(meeting_insights)"))
                columns = [row[1] for row in result.fetchall()]
                if 'created_at' in columns and 'generated_at' not in columns:
                    conn.execute(text(
                        "ALTER TABLE meeting_insights RENAME COLUMN created_at TO generated_at"
                    ))
                    logging.info("Migration v3->v4: Renamed created_at to generated_at")
                elif 'generated_at' in columns:
                    logging.info("Migration v3->v4: generated_at column already exists")
            except Exception as e:
                logging.error(f"Migration v3->v4 failed: {e}")
                raise

        if from_version < 5:
            try:
                result = conn.execute(text("PRAGMA table_info(meeting_chunks)"))
                columns = [row[1] for row in result.fetchall()]
                new_cols = [
                    ('status', "TEXT DEFAULT 'transcribed'"),
                    ('start_offset_sec', "REAL"),
                    ('end_offset_sec', "REAL"),
                    ('attempt_count', "INTEGER DEFAULT 0"),
                    ('last_error', "TEXT"),
                ]
                for col_name, col_def in new_cols:
                    if col_name not in columns:
                        conn.execute(text(
                            f"ALTER TABLE meeting_chunks ADD COLUMN {col_name} {col_def}"
                        ))
                        logging.info(f"Migration v4->v5: Added {col_name} to meeting_chunks")
            except Exception as e:
                logging.error(f"Migration v4->v5 failed: {e}")
                raise

        conn.execute(text("UPDATE schema_version SET version = :v"), {"v": SCHEMA_VERSION})
        logging.info(f"Database migrated to schema version {SCHEMA_VERSION}")

    # ------------------------------------------------------------------
    # JSON migration (legacy)
    # ------------------------------------------------------------------

    def _migrate_from_json(self) -> None:
        """Migrate existing JSON data to SQLite on first run."""
        history_file = getattr(config, 'HISTORY_FILE', 'transcription_history.json')
        meetings_file = getattr(config, 'MEETINGS_FILE', 'meetings.json')

        with self.get_session() as session:
            history_count = session.query(func.count(TranscriptionHistory.id)).scalar()
            meetings_count = session.query(func.count(Meeting.id)).scalar()

        if os.path.exists(history_file) and history_count == 0:
            self._migrate_history_from_json(history_file)
        if os.path.exists(meetings_file) and meetings_count == 0:
            self._migrate_meetings_from_json(meetings_file)

    def _migrate_history_from_json(self, json_path: str) -> None:
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            entries = data.get('entries', [])
            if not entries:
                logging.info("No history entries to migrate")
                return

            with self.get_session() as session:
                for entry in entries:
                    obj = TranscriptionHistory(
                        id=entry.get('id'),
                        text=entry.get('text', ''),
                        timestamp=entry.get('timestamp', ''),
                        model=entry.get('model', ''),
                        audio_file=entry.get('audio_file'),
                        transcription_time=entry.get('transcription_time'),
                        audio_duration=entry.get('audio_duration'),
                        file_size=entry.get('file_size'),
                    )
                    session.merge(obj)  # merge = INSERT OR UPDATE

            backup_path = json_path + '.bak'
            os.rename(json_path, backup_path)
            logging.info(f"Migrated {len(entries)} history entries from JSON. Backup: {backup_path}")
        except Exception as e:
            logging.error(f"Failed to migrate history from JSON: {e}")

    def _migrate_meetings_from_json(self, json_path: str) -> None:
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            meetings = data.get('meetings', [])
            if not meetings:
                logging.info("No meetings to migrate")
                return

            with self.get_session() as session:
                for m in meetings:
                    meeting = Meeting(
                        id=m.get('id'),
                        title=m.get('title', ''),
                        start_time=m.get('start_time', ''),
                        end_time=m.get('end_time'),
                        duration_seconds=m.get('duration_seconds', 0),
                        transcript=m.get('transcript', ''),
                        status=m.get('status', 'completed'),
                    )
                    session.merge(meeting)
                    for chunk in m.get('chunks', []):
                        session.add(MeetingChunk(
                            meeting_id=m.get('id'),
                            chunk_index=chunk.get('index', 0),
                            text=chunk.get('text', ''),
                            timestamp=chunk.get('timestamp', ''),
                            audio_file=chunk.get('audio_file'),
                        ))

            backup_path = json_path + '.bak'
            os.rename(json_path, backup_path)
            logging.info(f"Migrated {len(meetings)} meetings from JSON. Backup: {backup_path}")
        except Exception as e:
            logging.error(f"Failed to migrate meetings from JSON: {e}")

    # =====================================================================
    # Transcription History
    # =====================================================================

    def add_history_entry(
        self,
        entry_id: str,
        text: str,
        timestamp: str,
        model: str,
        audio_file: Optional[str] = None,
        transcription_time: Optional[float] = None,
        audio_duration: Optional[float] = None,
        file_size: Optional[int] = None,
    ) -> None:
        with self.get_session() as session:
            session.add(TranscriptionHistory(
                id=entry_id, text=text, timestamp=timestamp, model=model,
                audio_file=audio_file, transcription_time=transcription_time,
                audio_duration=audio_duration, file_size=file_size,
            ))

    def get_history_entries(self, limit: Optional[int] = None) -> List[TranscriptionHistory]:
        with self.get_session() as session:
            q = session.query(TranscriptionHistory).order_by(
                TranscriptionHistory.timestamp.desc()
            )
            if limit:
                q = q.limit(limit)
            return q.all()

    def get_history_entry_by_id(self, entry_id: str) -> Optional[TranscriptionHistory]:
        with self.get_session() as session:
            return session.get(TranscriptionHistory, entry_id)

    def delete_history_entry(self, entry_id: str) -> bool:
        with self.get_session() as session:
            entry = session.get(TranscriptionHistory, entry_id)
            if entry:
                session.delete(entry)
                return True
            return False

    def clear_history(self) -> None:
        with self.get_session() as session:
            session.query(TranscriptionHistory).delete()

    def update_history_audio_file(self, audio_filename: str) -> None:
        with self.get_session() as session:
            session.query(TranscriptionHistory).filter(
                TranscriptionHistory.audio_file == audio_filename
            ).update({TranscriptionHistory.audio_file: None})

    # =====================================================================
    # Meetings
    # =====================================================================

    def create_meeting(self, meeting_id: str, title: str, start_time: str) -> None:
        with self.get_session() as session:
            session.add(Meeting(
                id=meeting_id, title=title, start_time=start_time,
                status='in_progress',
            ))

    def add_meeting_chunk(
        self,
        meeting_id: str,
        chunk_index: int,
        text: str,
        timestamp: str,
        audio_file: Optional[str] = None,
    ) -> None:
        with self.get_session() as session:
            session.add(MeetingChunk(
                meeting_id=meeting_id, chunk_index=chunk_index,
                text=text, timestamp=timestamp, audio_file=audio_file,
            ))
            meeting = session.get(Meeting, meeting_id)
            if meeting:
                current = meeting.transcript or ''
                meeting.transcript = (current + ' ' + text).strip() if current else text

    def end_meeting(
        self,
        meeting_id: str,
        end_time: str,
        duration_seconds: float,
        audio_file: Optional[str] = None,
    ) -> None:
        with self.get_session() as session:
            meeting = session.get(Meeting, meeting_id)
            if meeting:
                meeting.end_time = end_time
                meeting.duration_seconds = duration_seconds
                meeting.status = 'completed'
                meeting.audio_file = audio_file

    def mark_meeting_interrupted(
        self, meeting_id: str, end_time: str, duration_seconds: float,
    ) -> None:
        with self.get_session() as session:
            meeting = session.get(Meeting, meeting_id)
            if meeting:
                meeting.end_time = end_time
                meeting.duration_seconds = duration_seconds
                meeting.status = 'interrupted'

    def get_meeting(self, meeting_id: str) -> Optional[Meeting]:
        with self.get_session() as session:
            return (
                session.query(Meeting)
                .options(joinedload(Meeting.chunks))
                .filter(Meeting.id == meeting_id)
                .first()
            )

    def get_all_meetings(self) -> List[Meeting]:
        with self.get_session() as session:
            return (
                session.query(Meeting)
                .options(joinedload(Meeting.chunks))
                .order_by(Meeting.start_time.desc())
                .all()
            )

    def get_in_progress_meetings(self) -> List[Meeting]:
        with self.get_session() as session:
            return (
                session.query(Meeting)
                .filter(Meeting.status == 'in_progress')
                .all()
            )

    def delete_meeting(self, meeting_id: str) -> bool:
        with self.get_session() as session:
            meeting = session.get(Meeting, meeting_id)
            if meeting:
                session.delete(meeting)  # CASCADE handles chunks + insights
                return True
            return False

    def update_meeting_title(self, meeting_id: str, title: str) -> bool:
        with self.get_session() as session:
            meeting = session.get(Meeting, meeting_id)
            if meeting:
                meeting.title = title
                return True
            return False

    def get_meeting_chunk_count(self, meeting_id: str) -> int:
        with self.get_session() as session:
            return (
                session.query(func.count(MeetingChunk.id))
                .filter(MeetingChunk.meeting_id == meeting_id)
                .scalar()
            )

    # -- Chunk spool (durable queue) ------------------------------------

    def register_spool_chunk(
        self,
        meeting_id: str,
        chunk_index: int,
        audio_file: str,
        start_offset_sec: float,
        end_offset_sec: float,
    ) -> None:
        with self.get_session() as session:
            session.add(MeetingChunk(
                meeting_id=meeting_id,
                chunk_index=chunk_index,
                text='',
                timestamp=datetime.now().isoformat(),
                audio_file=audio_file,
                status='pending',
                start_offset_sec=start_offset_sec,
                end_offset_sec=end_offset_sec,
                attempt_count=0,
            ))

    def mark_chunk_processing(self, meeting_id: str, chunk_index: int) -> None:
        with self.get_session() as session:
            chunk = (
                session.query(MeetingChunk)
                .filter_by(meeting_id=meeting_id, chunk_index=chunk_index)
                .first()
            )
            if chunk:
                chunk.status = 'processing'

    def update_chunk_transcribed(
        self, meeting_id: str, chunk_index: int, text: str,
    ) -> None:
        with self.get_session() as session:
            chunk = (
                session.query(MeetingChunk)
                .filter_by(meeting_id=meeting_id, chunk_index=chunk_index)
                .first()
            )
            if chunk:
                chunk.text = text
                chunk.status = 'transcribed'
                chunk.timestamp = datetime.now().isoformat()
                chunk.last_error = None

            # Rebuild entire meeting transcript deterministically
            transcribed = (
                session.query(MeetingChunk)
                .filter_by(meeting_id=meeting_id, status='transcribed')
                .order_by(MeetingChunk.chunk_index)
                .all()
            )
            transcript = " ".join(c.text for c in transcribed if c.text).strip()
            meeting = session.get(Meeting, meeting_id)
            if meeting:
                meeting.transcript = transcript

    def mark_chunk_failed(
        self, meeting_id: str, chunk_index: int, error: str,
    ) -> None:
        with self.get_session() as session:
            chunk = (
                session.query(MeetingChunk)
                .filter_by(meeting_id=meeting_id, chunk_index=chunk_index)
                .first()
            )
            if chunk:
                chunk.status = 'failed'
                chunk.attempt_count = (chunk.attempt_count or 0) + 1
                chunk.last_error = error

    def get_pending_chunks_for_meeting(self, meeting_id: str) -> List[MeetingChunk]:
        with self.get_session() as session:
            return (
                session.query(MeetingChunk)
                .filter_by(meeting_id=meeting_id, status='pending')
                .order_by(MeetingChunk.chunk_index)
                .all()
            )

    def reset_processing_chunks_to_pending(self, meeting_id: str) -> None:
        with self.get_session() as session:
            session.query(MeetingChunk).filter_by(
                meeting_id=meeting_id, status='processing',
            ).update({MeetingChunk.status: 'pending'})

    def get_meeting_audio_file(self, meeting_id: str) -> Optional[str]:
        with self.get_session() as session:
            meeting = session.get(Meeting, meeting_id)
            return meeting.audio_file if meeting else None

    # =====================================================================
    # Meeting Insights
    # =====================================================================

    def save_insight(
        self,
        meeting_id: str,
        insight_type: str,
        content: str,
        custom_prompt: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> int:
        generated_at = datetime.now().isoformat()
        with self.get_session() as session:
            # Upsert: find existing by (meeting_id, insight_type, custom_prompt)
            q = session.query(MeetingInsight).filter(
                MeetingInsight.meeting_id == meeting_id,
                MeetingInsight.insight_type == insight_type,
                func.coalesce(MeetingInsight.custom_prompt, '') == func.coalesce(custom_prompt, ''),
            )
            existing = q.first()

            if existing:
                existing.content = content
                existing.generated_at = generated_at
                existing.provider = provider
                existing.model = model
                session.flush()
                return existing.id
            else:
                insight = MeetingInsight(
                    meeting_id=meeting_id, insight_type=insight_type,
                    content=content, custom_prompt=custom_prompt,
                    generated_at=generated_at, provider=provider, model=model,
                )
                session.add(insight)
                session.flush()
                return insight.id

    def get_insight(
        self,
        meeting_id: str,
        insight_type: str,
        custom_prompt: Optional[str] = None,
    ) -> Optional[MeetingInsight]:
        with self.get_session() as session:
            return (
                session.query(MeetingInsight)
                .filter(
                    MeetingInsight.meeting_id == meeting_id,
                    MeetingInsight.insight_type == insight_type,
                    func.coalesce(MeetingInsight.custom_prompt, '') == func.coalesce(custom_prompt, ''),
                )
                .first()
            )

    def get_all_insights(self, meeting_id: str) -> List[MeetingInsight]:
        with self.get_session() as session:
            return (
                session.query(MeetingInsight)
                .filter(MeetingInsight.meeting_id == meeting_id)
                .order_by(MeetingInsight.generated_at.desc())
                .all()
            )

    def delete_insight(self, insight_id: int) -> bool:
        with self.get_session() as session:
            insight = session.get(MeetingInsight, insight_id)
            if insight:
                session.delete(insight)
                return True
            return False

    def has_insights(self, meeting_id: str) -> bool:
        with self.get_session() as session:
            return (
                session.query(MeetingInsight.id)
                .filter(MeetingInsight.meeting_id == meeting_id)
                .first()
            ) is not None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release all connections."""
        self.Session.remove()
        self.engine.dispose()


# Global database manager instance
db = DatabaseManager()
