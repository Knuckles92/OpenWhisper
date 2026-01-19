"""
Tests for SQLite database layer.
"""
import os
import json
import tempfile
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
        
        # Import here to avoid circular imports
        from services.database import DatabaseManager
        
        manager = DatabaseManager(db_path=db_path)
        yield manager
        manager.close()
    
    def test_schema_creation(self, temp_db):
        """Verify tables are created correctly on fresh DB."""
        with temp_db._get_connection() as conn:
            # Check transcription_history table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='transcription_history'"
            )
            assert cursor.fetchone() is not None
            
            # Check meetings table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meetings'"
            )
            assert cursor.fetchone() is not None
            
            # Check meeting_chunks table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meeting_chunks'"
            )
            assert cursor.fetchone() is not None
    
    def test_history_crud(self, temp_db):
        """Test history entry create, read, update, delete."""
        entry_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        
        # Create
        temp_db.add_history_entry(
            entry_id=entry_id,
            text="Test transcription",
            timestamp=timestamp,
            model="local_whisper",
            audio_file="test.wav",
            transcription_time=1.5,
            audio_duration=3.0,
            file_size=48000
        )
        
        # Read all
        entries = temp_db.get_history_entries()
        assert len(entries) == 1
        assert entries[0]['id'] == entry_id
        assert entries[0]['text'] == "Test transcription"
        
        # Read by ID
        entry = temp_db.get_history_entry_by_id(entry_id)
        assert entry is not None
        assert entry['model'] == "local_whisper"
        
        # Delete
        result = temp_db.delete_history_entry(entry_id)
        assert result is True
        
        # Verify deleted
        entry = temp_db.get_history_entry_by_id(entry_id)
        assert entry is None
    
    def test_meeting_crud(self, temp_db):
        """Test meeting create, add chunks, end, read, delete."""
        meeting_id = str(uuid.uuid4())
        start_time = datetime.now().isoformat()
        
        # Create meeting
        temp_db.create_meeting(meeting_id, "Test Meeting", start_time)
        
        # Add chunks
        temp_db.add_meeting_chunk(
            meeting_id=meeting_id,
            chunk_index=0,
            text="First chunk",
            timestamp=datetime.now().isoformat()
        )
        temp_db.add_meeting_chunk(
            meeting_id=meeting_id,
            chunk_index=1,
            text="Second chunk",
            timestamp=datetime.now().isoformat()
        )
        
        # Get meeting
        meeting = temp_db.get_meeting(meeting_id)
        assert meeting is not None
        assert meeting['title'] == "Test Meeting"
        assert len(meeting['chunks']) == 2
        assert "First chunk" in meeting['transcript']
        assert "Second chunk" in meeting['transcript']
        
        # End meeting
        temp_db.end_meeting(meeting_id, datetime.now().isoformat(), 120.5)
        
        meeting = temp_db.get_meeting(meeting_id)
        assert meeting['status'] == 'completed'
        assert meeting['duration_seconds'] == 120.5
        
        # Delete meeting (should cascade delete chunks)
        result = temp_db.delete_meeting(meeting_id)
        assert result is True
        
        # Verify deleted
        meeting = temp_db.get_meeting(meeting_id)
        assert meeting is None
        
        # Verify chunks deleted
        with temp_db._get_connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM meeting_chunks WHERE meeting_id = ?",
                (meeting_id,)
            )
            assert cursor.fetchone()[0] == 0
    
    def test_meeting_update_title(self, temp_db):
        """Test updating meeting title."""
        meeting_id = str(uuid.uuid4())
        temp_db.create_meeting(meeting_id, "Original Title", datetime.now().isoformat())
        
        result = temp_db.update_meeting_title(meeting_id, "New Title")
        assert result is True
        
        meeting = temp_db.get_meeting(meeting_id)
        assert meeting['title'] == "New Title"


class TestJsonMigration:
    """Tests for JSON to SQLite migration."""
    
    def test_history_migration(self, tmp_path):
        """Test migrating history from JSON file."""
        # Create mock JSON history file
        history_data = {
            'entries': [
                {
                    'id': str(uuid.uuid4()),
                    'text': 'Old transcription 1',
                    'timestamp': datetime.now().isoformat(),
                    'model': 'local_whisper',
                    'audio_file': None,
                    'transcription_time': 2.0,
                    'audio_duration': 5.0,
                    'file_size': 80000
                },
                {
                    'id': str(uuid.uuid4()),
                    'text': 'Old transcription 2',
                    'timestamp': datetime.now().isoformat(),
                    'model': 'api_whisper',
                    'audio_file': 'recording.wav',
                    'transcription_time': 1.0,
                    'audio_duration': 3.0,
                    'file_size': 48000
                }
            ]
        }
        
        json_path = tmp_path / "transcription_history.json"
        with open(json_path, 'w') as f:
            json.dump(history_data, f)
        
        # Create database manager - should auto-migrate
        db_path = str(tmp_path / "test.db")
        
        # Temporarily patch config
        import config
        original_history = config.config.HISTORY_FILE
        config.config.HISTORY_FILE = str(json_path)
        
        try:
            from services.database import DatabaseManager
            manager = DatabaseManager(db_path=db_path)
            
            # Verify migration
            entries = manager.get_history_entries()
            assert len(entries) == 2
            
            # Verify backup was created
            assert os.path.exists(str(json_path) + '.bak')
            
            manager.close()
        finally:
            config.config.HISTORY_FILE = original_history
    
    def test_meetings_migration(self, tmp_path):
        """Test migrating meetings from JSON file."""
        meeting_id = str(uuid.uuid4())
        # Create mock JSON meetings file
        meetings_data = {
            'meetings': [
                {
                    'id': meeting_id,
                    'title': 'Old Meeting',
                    'start_time': datetime.now().isoformat(),
                    'end_time': datetime.now().isoformat(),
                    'duration_seconds': 300.0,
                    'transcript': 'Chunk 1 Chunk 2',
                    'status': 'completed',
                    'chunks': [
                        {
                            'index': 0,
                            'text': 'Chunk 1',
                            'timestamp': datetime.now().isoformat(),
                            'audio_file': None
                        },
                        {
                            'index': 1,
                            'text': 'Chunk 2',
                            'timestamp': datetime.now().isoformat(),
                            'audio_file': None
                        }
                    ]
                }
            ]
        }
        
        json_path = tmp_path / "meetings.json"
        with open(json_path, 'w') as f:
            json.dump(meetings_data, f)
        
        # Create database manager - should auto-migrate
        db_path = str(tmp_path / "test.db")
        
        # Temporarily patch config
        import config
        original_meetings = config.config.MEETINGS_FILE
        config.config.MEETINGS_FILE = str(json_path)
        
        try:
            from services.database import DatabaseManager
            manager = DatabaseManager(db_path=db_path)
            
            # Verify migration
            meetings = manager.get_all_meetings()
            assert len(meetings) == 1
            
            meeting = manager.get_meeting(meeting_id)
            assert meeting['title'] == 'Old Meeting'
            assert len(meeting['chunks']) == 2
            
            # Verify backup was created
            assert os.path.exists(str(json_path) + '.bak')
            
            manager.close()
        finally:
            config.config.MEETINGS_FILE = original_meetings


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
