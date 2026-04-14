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
        assert "meetings" in table_names
        assert "meeting_chunks" in table_names
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

    def test_meeting_crud(self, temp_db):
        """Test meeting create, add chunks, end, read, delete."""
        meeting_id = str(uuid.uuid4())
        start_time = datetime.now().isoformat()

        temp_db.create_meeting(meeting_id, "Test Meeting", start_time)
        temp_db.add_meeting_chunk(
            meeting_id=meeting_id,
            chunk_index=0,
            text="First chunk",
            timestamp=datetime.now().isoformat(),
        )
        temp_db.add_meeting_chunk(
            meeting_id=meeting_id,
            chunk_index=1,
            text="Second chunk",
            timestamp=datetime.now().isoformat(),
        )

        meeting = temp_db.get_meeting(meeting_id)
        assert meeting is not None
        assert meeting.title == "Test Meeting"
        assert len(meeting.chunks) == 2
        assert "First chunk" in meeting.transcript
        assert "Second chunk" in meeting.transcript

        temp_db.end_meeting(meeting_id, datetime.now().isoformat(), 120.5)

        meeting = temp_db.get_meeting(meeting_id)
        assert meeting.status == "completed"
        assert meeting.duration_seconds == 120.5

        result = temp_db.delete_meeting(meeting_id)
        assert result is True

        meeting = temp_db.get_meeting(meeting_id)
        assert meeting is None
        assert temp_db.get_meeting_chunk_count(meeting_id) == 0

    def test_meeting_update_title(self, temp_db):
        """Test updating meeting title."""
        meeting_id = str(uuid.uuid4())
        temp_db.create_meeting(meeting_id, "Original Title", datetime.now().isoformat())

        result = temp_db.update_meeting_title(meeting_id, "New Title")
        assert result is True

        meeting = temp_db.get_meeting(meeting_id)
        assert meeting.title == "New Title"

    def test_migration_removes_legacy_insights_table(self, tmp_path):
        """Verify schema v6 drops the legacy insights table."""
        db_path = str(tmp_path / "legacy.db")

        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO schema_version(version) VALUES (5)")
            conn.execute("CREATE TABLE meetings (id TEXT PRIMARY KEY)")
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
            assert version == 6
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

    def test_meetings_migration(self, tmp_path):
        """Test migrating meetings from JSON file."""
        meeting_id = str(uuid.uuid4())
        meetings_data = {
            "meetings": [
                {
                    "id": meeting_id,
                    "title": "Old Meeting",
                    "start_time": datetime.now().isoformat(),
                    "end_time": datetime.now().isoformat(),
                    "duration_seconds": 300.0,
                    "transcript": "Chunk 1 Chunk 2",
                    "status": "completed",
                    "chunks": [
                        {
                            "index": 0,
                            "text": "Chunk 1",
                            "timestamp": datetime.now().isoformat(),
                            "audio_file": None,
                        },
                        {
                            "index": 1,
                            "text": "Chunk 2",
                            "timestamp": datetime.now().isoformat(),
                            "audio_file": None,
                        },
                    ],
                }
            ]
        }

        json_path = tmp_path / "meetings.json"
        with open(json_path, "w") as f:
            json.dump(meetings_data, f)

        db_path = str(tmp_path / "test.db")

        import config

        original_meetings = config.config.MEETINGS_FILE
        config.config.MEETINGS_FILE = str(json_path)

        try:
            from services.database import DatabaseManager

            manager = DatabaseManager(db_path=db_path)
            meetings = manager.get_all_meetings()
            assert len(meetings) == 1

            meeting = manager.get_meeting(meeting_id)
            assert meeting.title == "Old Meeting"
            assert len(meeting.chunks) == 2
            assert os.path.exists(str(json_path) + ".bak")
            manager.close()
        finally:
            config.config.MEETINGS_FILE = original_meetings


class TestMeetingStorage:
    """Tests for meeting storage display formatting."""

    def test_get_meetings_for_display_excludes_has_insights(self, tmp_path):
        """Meeting display rows should no longer expose insights metadata."""
        db_path = str(tmp_path / "display.db")

        from services.database import DatabaseManager
        from services import meeting_storage as meeting_storage_module
        from services.meeting_storage import MeetingStorage

        original_db = meeting_storage_module.db
        temp_db = DatabaseManager(db_path=db_path)
        meeting_storage_module.db = temp_db

        try:
            storage = MeetingStorage()
            meeting_id = str(uuid.uuid4())
            temp_db.create_meeting(meeting_id, "Display Test", datetime.now().isoformat())
            temp_db.add_meeting_chunk(
                meeting_id=meeting_id,
                chunk_index=0,
                text="Transcript preview for display formatting.",
                timestamp=datetime.now().isoformat(),
            )
            temp_db.end_meeting(meeting_id, datetime.now().isoformat(), 42.0)

            meetings = storage.get_meetings_for_display()

            assert len(meetings) == 1
            assert meetings[0]["title"] == "Display Test"
            assert "has_insights" not in meetings[0]
        finally:
            meeting_storage_module.db = original_db
            temp_db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
