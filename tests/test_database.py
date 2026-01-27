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


class TestMeetingInsights:
    """Tests for the meeting insights functionality."""

    @pytest.fixture
    def temp_db(self, tmp_path):
        """Create a temporary database for testing."""
        db_path = str(tmp_path / "test.db")
        from services.database import DatabaseManager
        manager = DatabaseManager(db_path=db_path)
        yield manager
        manager.close()

    @pytest.fixture
    def meeting_with_transcript(self, temp_db):
        """Create a meeting with transcript for testing insights."""
        meeting_id = str(uuid.uuid4())
        start_time = datetime.now().isoformat()
        temp_db.create_meeting(meeting_id, "Test Meeting", start_time)
        temp_db.add_meeting_chunk(
            meeting_id=meeting_id,
            chunk_index=0,
            text="This is a test transcript.",
            timestamp=datetime.now().isoformat()
        )
        temp_db.end_meeting(meeting_id, datetime.now().isoformat(), 60.0)
        return meeting_id

    def test_save_insight(self, temp_db, meeting_with_transcript):
        """Test saving a new insight."""
        meeting_id = meeting_with_transcript

        insight_id = temp_db.save_insight(
            meeting_id=meeting_id,
            insight_type='summary',
            content='This is a test summary.',
            custom_prompt=None,
            provider='openai',
            model='gpt-4o'
        )

        assert insight_id is not None
        assert insight_id > 0

    def test_get_insight(self, temp_db, meeting_with_transcript):
        """Test retrieving a saved insight."""
        meeting_id = meeting_with_transcript

        temp_db.save_insight(
            meeting_id=meeting_id,
            insight_type='summary',
            content='This is a test summary.',
            provider='openai',
            model='gpt-4o'
        )

        insight = temp_db.get_insight(meeting_id, 'summary')

        assert insight is not None
        assert insight['meeting_id'] == meeting_id
        assert insight['insight_type'] == 'summary'
        assert insight['content'] == 'This is a test summary.'
        assert insight['provider'] == 'openai'
        assert insight['model'] == 'gpt-4o'
        assert insight['generated_at'] is not None

    def test_get_nonexistent_insight(self, temp_db, meeting_with_transcript):
        """Test retrieving a nonexistent insight returns None."""
        meeting_id = meeting_with_transcript
        insight = temp_db.get_insight(meeting_id, 'summary')
        assert insight is None

    def test_save_custom_insight_with_prompt(self, temp_db, meeting_with_transcript):
        """Test saving a custom insight with a custom prompt."""
        meeting_id = meeting_with_transcript

        temp_db.save_insight(
            meeting_id=meeting_id,
            insight_type='custom',
            content='Custom analysis result.',
            custom_prompt='What were the main topics?',
            provider='openrouter',
            model='claude-3'
        )

        insight = temp_db.get_insight(meeting_id, 'custom', custom_prompt='What were the main topics?')

        assert insight is not None
        assert insight['custom_prompt'] == 'What were the main topics?'
        assert insight['content'] == 'Custom analysis result.'

    def test_upsert_insight(self, temp_db, meeting_with_transcript):
        """Test that saving an insight with the same type updates it."""
        meeting_id = meeting_with_transcript

        # Save initial insight
        temp_db.save_insight(
            meeting_id=meeting_id,
            insight_type='summary',
            content='Initial summary.',
            provider='openai'
        )

        # Save updated insight
        temp_db.save_insight(
            meeting_id=meeting_id,
            insight_type='summary',
            content='Updated summary.',
            provider='openai'
        )

        # Verify only one insight exists and it's updated
        insight = temp_db.get_insight(meeting_id, 'summary')
        assert insight['content'] == 'Updated summary.'

        all_insights = temp_db.get_all_insights(meeting_id)
        assert len(all_insights) == 1

    def test_get_all_insights(self, temp_db, meeting_with_transcript):
        """Test retrieving all insights for a meeting."""
        meeting_id = meeting_with_transcript

        # Save multiple insights
        temp_db.save_insight(meeting_id, 'summary', 'Summary content.')
        temp_db.save_insight(meeting_id, 'action_items', 'Action items content.')
        temp_db.save_insight(meeting_id, 'custom', 'Custom content.', 'Custom prompt 1')
        temp_db.save_insight(meeting_id, 'custom', 'Another custom content.', 'Custom prompt 2')

        all_insights = temp_db.get_all_insights(meeting_id)

        assert len(all_insights) == 4
        types = [i['insight_type'] for i in all_insights]
        assert 'summary' in types
        assert 'action_items' in types
        assert types.count('custom') == 2

    def test_has_insights(self, temp_db, meeting_with_transcript):
        """Test checking if a meeting has insights."""
        meeting_id = meeting_with_transcript

        # Initially no insights
        assert temp_db.has_insights(meeting_id) is False

        # Add insight
        temp_db.save_insight(meeting_id, 'summary', 'Summary content.')

        # Now has insights
        assert temp_db.has_insights(meeting_id) is True

    def test_delete_insight(self, temp_db, meeting_with_transcript):
        """Test deleting an insight."""
        meeting_id = meeting_with_transcript

        insight_id = temp_db.save_insight(
            meeting_id=meeting_id,
            insight_type='summary',
            content='To be deleted.'
        )

        result = temp_db.delete_insight(insight_id)
        assert result is True

        insight = temp_db.get_insight(meeting_id, 'summary')
        assert insight is None

    def test_delete_nonexistent_insight(self, temp_db):
        """Test deleting a nonexistent insight returns False."""
        result = temp_db.delete_insight(99999)
        assert result is False

    def test_cascade_delete_meeting_removes_insights(self, temp_db, meeting_with_transcript):
        """Test that deleting a meeting also deletes its insights."""
        meeting_id = meeting_with_transcript

        # Add insights
        temp_db.save_insight(meeting_id, 'summary', 'Summary content.')
        temp_db.save_insight(meeting_id, 'action_items', 'Action items content.')

        # Verify insights exist
        assert temp_db.has_insights(meeting_id) is True

        # Delete meeting
        temp_db.delete_meeting(meeting_id)

        # Verify insights are deleted
        assert temp_db.has_insights(meeting_id) is False
        assert temp_db.get_all_insights(meeting_id) == []

    def test_unique_constraint_different_custom_prompts(self, temp_db, meeting_with_transcript):
        """Test that different custom prompts create different insights."""
        meeting_id = meeting_with_transcript

        # Save two custom insights with different prompts
        temp_db.save_insight(meeting_id, 'custom', 'Result 1', 'Prompt 1')
        temp_db.save_insight(meeting_id, 'custom', 'Result 2', 'Prompt 2')

        # Both should exist
        insight1 = temp_db.get_insight(meeting_id, 'custom', 'Prompt 1')
        insight2 = temp_db.get_insight(meeting_id, 'custom', 'Prompt 2')

        assert insight1 is not None
        assert insight2 is not None
        assert insight1['content'] == 'Result 1'
        assert insight2['content'] == 'Result 2'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
