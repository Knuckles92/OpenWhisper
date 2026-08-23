"""SQLite persistence and schema migrations."""
import json
import logging
import os
from contextlib import contextmanager
from typing import List, Optional

from sqlalchemy import create_engine, event, func, inspect, text
from sqlalchemy.orm import scoped_session, sessionmaker

from config import config
from services.models import (
    Base, SchemaVersion, TranscriptionHistory,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 12


class DatabaseManager:
    """Manages SQLite database for transcription history storage."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or getattr(config, 'DATABASE_FILE', 'openwhisper.db')

        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
        )

        # Enable foreign keys for every raw SQLite connection. WAL journaling
        # is required by Meeting Mode: capture-thread writes, web-server reads,
        # and Qt reads run concurrently, and DELETE-mode journaling produces
        # "database is locked" errors under that load.
        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        self._session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False,
        )
        self.Session = scoped_session(self._session_factory)

        self._init_database()
        self._migrate_from_json()

        logger.info(f"DatabaseManager initialized: {self.db_path}")

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

    def _init_database(self) -> None:
        # For existing databases, run migrations BEFORE create_all so that
        # ALTER TABLE statements can add columns that models expect.
        self._maybe_run_migrations()

        Base.metadata.create_all(self.engine)
        self._drop_removed_meeting_tables()
        self._ensure_meeting_fts()

        with self.get_session() as session:
            version_row = session.get(SchemaVersion, SCHEMA_VERSION)
            if not version_row:
                session.query(SchemaVersion).delete()
                session.add(SchemaVersion(version=SCHEMA_VERSION))

        logger.info("Database schema initialized")

    def _drop_removed_meeting_tables(self) -> None:
        """Drop meeting-mode tables that may exist from older app versions.

        Safe to remove once schema v9 ships and all users have migrated past v8.
        Track removal target: 2026-12-01.
        """
        with self.engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS idx_chunks_meeting_id"))
            conn.execute(text("DROP INDEX IF EXISTS idx_meetings_start_time"))
            conn.execute(text("DROP INDEX IF EXISTS idx_insights_unique"))
            conn.execute(text("DROP INDEX IF EXISTS idx_insights_meeting_id"))
            conn.execute(text("DROP TABLE IF EXISTS meeting_insights"))
            conn.execute(text("DROP TABLE IF EXISTS meeting_chunks"))
            conn.execute(text("DROP TABLE IF EXISTS meetings"))

    def _ensure_meeting_fts(self) -> None:
        """Create the meeting-transcript FTS5 table and its sync triggers.

        FTS virtual tables cannot be expressed as ORM models, so this runs as
        raw SQL after ``create_all`` on both fresh and migrated databases.
        """
        statements = [
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS meeting_segments_fts USING fts5(
                text, content='meeting_segments', content_rowid='rowid'
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS meeting_segments_fts_ai
            AFTER INSERT ON meeting_segments BEGIN
                INSERT INTO meeting_segments_fts(rowid, text)
                VALUES (new.rowid, new.text);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS meeting_segments_fts_ad
            AFTER DELETE ON meeting_segments BEGIN
                INSERT INTO meeting_segments_fts(meeting_segments_fts, rowid, text)
                VALUES ('delete', old.rowid, old.text);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS meeting_segments_fts_au
            AFTER UPDATE OF text ON meeting_segments BEGIN
                INSERT INTO meeting_segments_fts(meeting_segments_fts, rowid, text)
                VALUES ('delete', old.rowid, old.text);
                INSERT INTO meeting_segments_fts(rowid, text)
                VALUES (new.rowid, new.text);
            END
            """,
        ]
        try:
            with self.engine.begin() as conn:
                for statement in statements:
                    conn.execute(text(statement))
        except Exception as e:
            # FTS5 is compiled into every mainstream SQLite build; if it is
            # missing, meeting search degrades but nothing else breaks.
            logger.warning(f"Could not initialize meeting FTS index: {e}")

    def _maybe_run_migrations(self) -> None:
        insp = inspect(self.engine)
        if not insp.has_table('schema_version'):
            return

        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT version FROM schema_version LIMIT 1")).fetchone()
            if row is None:
                return
            current_version = row[0]
            if current_version < SCHEMA_VERSION:
                self._run_migrations(conn, current_version)
                conn.commit()

            conn.commit()

    def _run_migrations(self, conn, from_version: int) -> None:
        logger.info(f"Running database migrations from v{from_version} to v{SCHEMA_VERSION}")

        if from_version < 6:
            try:
                conn.execute(text("DROP INDEX IF EXISTS idx_insights_unique"))
                conn.execute(text("DROP INDEX IF EXISTS idx_insights_meeting_id"))
                conn.execute(text("DROP TABLE IF EXISTS meeting_insights"))
                logger.info("Migration v5->v6: Removed meeting_insights table")
            except Exception as e:
                logger.error(f"Migration v5->v6 failed: {e}")
                raise

        if from_version < 7:
            try:
                conn.execute(text("DROP INDEX IF EXISTS idx_chunks_meeting_id"))
                conn.execute(text("DROP INDEX IF EXISTS idx_meetings_start_time"))
                conn.execute(text("DROP INDEX IF EXISTS idx_insights_unique"))
                conn.execute(text("DROP INDEX IF EXISTS idx_insights_meeting_id"))
                conn.execute(text("DROP TABLE IF EXISTS meeting_insights"))
                conn.execute(text("DROP TABLE IF EXISTS meeting_chunks"))
                conn.execute(text("DROP TABLE IF EXISTS meetings"))
                logger.info("Migration v6->v7: Removed meeting mode tables")
            except Exception as e:
                logger.error(f"Migration v6->v7 failed: {e}")
                raise

        if from_version < 8:
            try:
                table_exists = conn.execute(
                    text(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='transcription_history'"
                    )
                ).fetchone()
                if table_exists:
                    columns = {
                        row[1]
                        for row in conn.execute(
                            text("PRAGMA table_info(transcription_history)")
                        ).fetchall()
                    }
                    if "raw_text" not in columns:
                        conn.execute(
                            text(
                                "ALTER TABLE transcription_history "
                                "ADD COLUMN raw_text TEXT"
                            )
                        )
                logger.info("Migration v7->v8: Added raw_text to transcription_history")
            except Exception as e:
                logger.error(f"Migration v7->v8 failed: {e}")
                raise

        if from_version < 9:
            try:
                table_exists = conn.execute(
                    text(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='transcription_history'"
                    )
                ).fetchone()
                if table_exists:
                    columns = {
                        row[1]
                        for row in conn.execute(
                            text("PRAGMA table_info(transcription_history)")
                        ).fetchall()
                    }
                    for column in ("cleanup_provider", "cleanup_model"):
                        if column not in columns:
                            conn.execute(
                                text(
                                    "ALTER TABLE transcription_history "
                                    f"ADD COLUMN {column} TEXT"
                                )
                            )
                logger.info(
                    "Migration v8->v9: Added cleanup_provider/cleanup_model "
                    "to transcription_history"
                )
            except Exception as e:
                logger.error(f"Migration v8->v9 failed: {e}")
                raise

        if from_version < 10:
            # Meeting Mode tables (meeting_sessions, meeting_audio_chunks,
            # meeting_segments, meeting_participants, meeting_state_items,
            # meeting_questions, meeting_events) are new in v10 and created by
            # Base.metadata.create_all after migrations run; the FTS index is
            # created by _ensure_meeting_fts. Nothing to transform here.
            logger.info("Migration v9->v10: Meeting Mode tables added via create_all")

        if from_version < 11:
            try:
                table_exists = conn.execute(
                    text(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='meeting_sessions'"
                    )
                ).fetchone()
                if table_exists:
                    columns = {
                        row[1]
                        for row in conn.execute(
                            text("PRAGMA table_info(meeting_sessions)")
                        ).fetchall()
                    }
                    if "agent_endpoint_json" not in columns:
                        conn.execute(
                            text(
                                "ALTER TABLE meeting_sessions "
                                "ADD COLUMN agent_endpoint_json TEXT"
                            )
                        )
                logger.info(
                    "Migration v10->v11: Added agent_endpoint_json "
                    "to meeting_sessions"
                )
            except Exception as e:
                logger.error("Migration v10->v11 failed: %s", e)
                raise

        if from_version < 12:
            try:
                table_exists = conn.execute(
                    text(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='transcription_history'"
                    )
                ).fetchone()
                if table_exists:
                    columns = {
                        row[1]
                        for row in conn.execute(
                            text("PRAGMA table_info(transcription_history)")
                        ).fetchall()
                    }
                    if "source_name" not in columns:
                        conn.execute(
                            text(
                                "ALTER TABLE transcription_history "
                                "ADD COLUMN source_name TEXT"
                            )
                        )
                logger.info(
                    "Migration v11->v12: Added source_name to transcription_history"
                )
            except Exception as e:
                logger.error("Migration v11->v12 failed: %s", e)
                raise

        conn.execute(text("UPDATE schema_version SET version = :v"), {"v": SCHEMA_VERSION})
        logger.info(f"Database migrated to schema version {SCHEMA_VERSION}")

    def _migrate_from_json(self) -> None:
        """Migrate existing JSON data to SQLite on first run."""
        history_file = getattr(config, 'HISTORY_FILE', 'transcription_history.json')
        with self.get_session() as session:
            history_count = session.query(func.count(TranscriptionHistory.id)).scalar()

        if os.path.exists(history_file) and history_count == 0:
            self._migrate_history_from_json(history_file)

    def _migrate_history_from_json(self, json_path: str) -> None:
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            entries = data.get('entries', [])
            if not entries:
                logger.info("No history entries to migrate")
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
                    session.merge(obj)

            backup_path = json_path + '.bak'
            os.rename(json_path, backup_path)
            logger.info(f"Migrated {len(entries)} history entries from JSON. Backup: {backup_path}")
        except Exception as e:
            logger.error(f"Failed to migrate history from JSON: {e}")

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
        raw_text: Optional[str] = None,
        cleanup_provider: Optional[str] = None,
        cleanup_model: Optional[str] = None,
        source_name: Optional[str] = None,
    ) -> None:
        with self.get_session() as session:
            session.add(TranscriptionHistory(
                id=entry_id, text=text, raw_text=raw_text,
                timestamp=timestamp, model=model,
                audio_file=audio_file, transcription_time=transcription_time,
                audio_duration=audio_duration, file_size=file_size,
                cleanup_provider=cleanup_provider, cleanup_model=cleanup_model,
                source_name=source_name,
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

    def clear_history_audio_file(self, audio_file: str) -> None:
        """Clear the audio_file reference on history entries matching a filename."""
        with self.get_session() as session:
            session.query(TranscriptionHistory).filter(
                TranscriptionHistory.audio_file == audio_file
            ).update({TranscriptionHistory.audio_file: None})

    def close(self) -> None:
        """Release all connections."""
        self.Session.remove()
        self.engine.dispose()


class _LazyDatabaseManager:
    def __init__(self) -> None:
        self._instance: Optional[DatabaseManager] = None

    def _get_instance(self) -> DatabaseManager:
        if self._instance is None:
            self._instance = DatabaseManager()
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._get_instance(), name)

    def close(self) -> None:
        if self._instance is not None:
            self._instance.close()
            self._instance = None

db = _LazyDatabaseManager()
