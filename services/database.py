"""
SQLite database manager for transcription history.
Provides unified data persistence via SQLAlchemy ORM with migration support.
"""
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

# Schema version for future migrations
SCHEMA_VERSION = 7


class DatabaseManager:
    """Manages SQLite database for transcription history storage."""

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
        self._drop_removed_meeting_tables()

        # Ensure schema_version row exists
        with self.get_session() as session:
            version_row = session.get(SchemaVersion, SCHEMA_VERSION)
            if not version_row:
                # Clear any old version rows and set current
                session.query(SchemaVersion).delete()
                session.add(SchemaVersion(version=SCHEMA_VERSION))

        logging.info("Database schema initialized")

    def _drop_removed_meeting_tables(self) -> None:
        """Drop meeting-mode tables that may exist from older app versions.

        Safe to remove once schema v8 ships and all users have migrated past v7.
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

            conn.commit()

    def _run_migrations(self, conn, from_version: int) -> None:
        """Run progressive migrations using raw SQL (standard for non-Alembic projects)."""
        logging.info(f"Running database migrations from v{from_version} to v{SCHEMA_VERSION}")

        if from_version < 6:
            try:
                conn.execute(text("DROP INDEX IF EXISTS idx_insights_unique"))
                conn.execute(text("DROP INDEX IF EXISTS idx_insights_meeting_id"))
                conn.execute(text("DROP TABLE IF EXISTS meeting_insights"))
                logging.info("Migration v5->v6: Removed meeting_insights table")
            except Exception as e:
                logging.error(f"Migration v5->v6 failed: {e}")
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
                logging.info("Migration v6->v7: Removed meeting mode tables")
            except Exception as e:
                logging.error(f"Migration v6->v7 failed: {e}")
                raise

        conn.execute(text("UPDATE schema_version SET version = :v"), {"v": SCHEMA_VERSION})
        logging.info(f"Database migrated to schema version {SCHEMA_VERSION}")

    # ------------------------------------------------------------------
    # JSON migration (legacy)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release all connections."""
        self.Session.remove()
        self.engine.dispose()


class _LazyDatabaseManager:
    """Create the database manager only when persistence is first used."""

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


# Public lazy database manager proxy.
db = _LazyDatabaseManager()
