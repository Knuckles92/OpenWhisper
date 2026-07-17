"""
Tests for SQLAlchemy database layer.
"""
import json
import os
import pytest
import uuid
from datetime import datetime

# Add parent directory to path for imports
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDatabaseManager:
    """Tests for the DatabaseManager class."""

    @pytest.fixture
    def temp_db(self, tmp_path):
        """Create a temporary database for testing."""
        db_path = str(tmp_path / "test.db")

        from services.database import DatabaseManager

        manager = DatabaseManager(db_path=db_path)
        yield manager
        manager.close()

    def test_schema_creation(self, temp_db):
        """Verify tables are created correctly on fresh DB."""
        from sqlalchemy import inspect

        insp = inspect(temp_db.engine)
        table_names = insp.get_table_names()

        assert "schema_version" in table_names
        assert "transcription_history" in table_names
        assert "meetings" not in table_names
        assert "meeting_chunks" not in table_names
        assert "meeting_insights" not in table_names

    def test_history_crud(self, temp_db):
        """Test history entry create, read, update, delete."""
        entry_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()

        temp_db.add_history_entry(
            entry_id=entry_id,
            text="Test transcription",
            timestamp=timestamp,
            model="local_whisper",
            audio_file="test.wav",
            transcription_time=1.5,
            audio_duration=3.0,
            file_size=48000,
        )

        entries = temp_db.get_history_entries()
        assert len(entries) == 1
        assert entries[0].id == entry_id
        assert entries[0].text == "Test transcription"

        entry = temp_db.get_history_entry_by_id(entry_id)
        assert entry is not None
        assert entry.model == "local_whisper"

        result = temp_db.delete_history_entry(entry_id)
        assert result is True

        entry = temp_db.get_history_entry_by_id(entry_id)
        assert entry is None

    def test_migration_removes_meeting_tables(self, tmp_path):
        """Verify schema v7 drops all meeting-mode tables."""
        db_path = str(tmp_path / "legacy.db")

        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO schema_version(version) VALUES (5)")
            conn.execute(
                """
                CREATE TABLE meetings (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    start_time TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE meeting_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE meeting_insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id TEXT NOT NULL,
                    insight_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    custom_prompt TEXT,
                    generated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX idx_insights_meeting_id ON meeting_insights(meeting_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX idx_insights_unique "
                "ON meeting_insights(meeting_id, insight_type, COALESCE(custom_prompt, ''))"
            )
            conn.execute("CREATE INDEX idx_meetings_start_time ON meetings(start_time)")
            conn.execute("CREATE INDEX idx_chunks_meeting_id ON meeting_chunks(meeting_id)")
            conn.commit()
        finally:
            conn.close()

        from services.database import DatabaseManager

        manager = DatabaseManager(db_path=db_path)
        try:
            with manager.engine.connect() as connection:
                tables = {
                    row[0]
                    for row in connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                version = connection.exec_driver_sql(
                    "SELECT version FROM schema_version"
                ).scalar_one()

            assert "meeting_insights" not in tables
            assert "meeting_chunks" not in tables
            assert "meetings" not in tables
            assert version == 9
        finally:
            manager.close()

    def test_history_raw_text_crud(self, temp_db):
        """History entries can store distinct raw and fixed text."""
        entry_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()

        temp_db.add_history_entry(
            entry_id=entry_id,
            text="Fixed transcription.",
            timestamp=timestamp,
            model="local_whisper",
            raw_text="um fixed transcription",
        )

        entry = temp_db.get_history_entry_by_id(entry_id)
        assert entry is not None
        assert entry.text == "Fixed transcription."
        assert entry.raw_text == "um fixed transcription"

    def test_migration_adds_raw_text_column(self, tmp_path):
        """Verify schema v8 adds raw_text to transcription_history."""
        db_path = str(tmp_path / "legacy_v7.db")

        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO schema_version(version) VALUES (7)")
            conn.execute(
                """
                CREATE TABLE transcription_history (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    model TEXT NOT NULL,
                    audio_file TEXT,
                    transcription_time REAL,
                    audio_duration REAL,
                    file_size INTEGER
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        from services.database import DatabaseManager, SCHEMA_VERSION

        manager = DatabaseManager(db_path=db_path)
        try:
            with manager.engine.connect() as connection:
                columns = {
                    row[1]
                    for row in connection.exec_driver_sql(
                        "PRAGMA table_info(transcription_history)"
                    )
                }
                version = connection.exec_driver_sql(
                    "SELECT version FROM schema_version"
                ).scalar_one()

            assert "raw_text" in columns
            assert "cleanup_provider" in columns
            assert "cleanup_model" in columns
            assert version == SCHEMA_VERSION == 9
        finally:
            manager.close()

    def test_history_cleanup_metadata_crud(self, temp_db):
        """History entries can store the cleanup provider/model used."""
        entry_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()

        temp_db.add_history_entry(
            entry_id=entry_id,
            text="Fixed transcription.",
            timestamp=timestamp,
            model="local_whisper",
            raw_text="um fixed transcription",
            cleanup_provider="openai",
            cleanup_model="gpt-4.1-mini",
        )

        entry = temp_db.get_history_entry_by_id(entry_id)
        assert entry is not None
        assert entry.cleanup_provider == "openai"
        assert entry.cleanup_model == "gpt-4.1-mini"

    def test_migration_adds_cleanup_columns(self, tmp_path):
        """Verify schema v9 adds cleanup_provider/cleanup_model to a v8 DB."""
        db_path = str(tmp_path / "legacy_v8.db")

        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO schema_version(version) VALUES (8)")
            conn.execute(
                """
                CREATE TABLE transcription_history (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    raw_text TEXT,
                    timestamp TEXT NOT NULL,
                    model TEXT NOT NULL,
                    audio_file TEXT,
                    transcription_time REAL,
                    audio_duration REAL,
                    file_size INTEGER
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        from services.database import DatabaseManager, SCHEMA_VERSION

        manager = DatabaseManager(db_path=db_path)
        try:
            with manager.engine.connect() as connection:
                columns = {
                    row[1]
                    for row in connection.exec_driver_sql(
                        "PRAGMA table_info(transcription_history)"
                    )
                }
                version = connection.exec_driver_sql(
                    "SELECT version FROM schema_version"
                ).scalar_one()

            assert "cleanup_provider" in columns
            assert "cleanup_model" in columns
            assert version == SCHEMA_VERSION == 9
        finally:
            manager.close()


class TestJsonMigration:
    """Tests for JSON to SQLite migration."""

    def test_history_migration(self, tmp_path):
        """Test migrating history from JSON file."""
        history_data = {
            "entries": [
                {
                    "id": str(uuid.uuid4()),
                    "text": "Old transcription 1",
                    "timestamp": datetime.now().isoformat(),
                    "model": "local_whisper",
                    "audio_file": None,
                    "transcription_time": 2.0,
                    "audio_duration": 5.0,
                    "file_size": 80000,
                },
                {
                    "id": str(uuid.uuid4()),
                    "text": "Old transcription 2",
                    "timestamp": datetime.now().isoformat(),
                    "model": "api_whisper",
                    "audio_file": "recording.wav",
                    "transcription_time": 1.0,
                    "audio_duration": 3.0,
                    "file_size": 48000,
                },
            ]
        }

        json_path = tmp_path / "transcription_history.json"
        with open(json_path, "w") as f:
            json.dump(history_data, f)

        db_path = str(tmp_path / "test.db")

        import config

        original_history = config.config.HISTORY_FILE
        config.config.HISTORY_FILE = str(json_path)

        try:
            from services.database import DatabaseManager

            manager = DatabaseManager(db_path=db_path)
            entries = manager.get_history_entries()
            assert len(entries) == 2
            assert os.path.exists(str(json_path) + ".bak")
            manager.close()
        finally:
            config.config.HISTORY_FILE = original_history

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
