"""
SQLite database manager for transcription history and meeting storage.
Provides unified data persistence with migration support from JSON files.
"""
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import config


# Schema version for future migrations
SCHEMA_VERSION = 4

SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- Transcription history table
CREATE TABLE IF NOT EXISTS transcription_history (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    audio_file TEXT,
    transcription_time REAL,
    audio_duration REAL,
    file_size INTEGER
);

-- Meetings table
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    duration_seconds REAL DEFAULT 0,
    transcript TEXT DEFAULT '',
    status TEXT DEFAULT 'in_progress'
);

-- Meeting chunks table (one-to-many with meetings)
CREATE TABLE IF NOT EXISTS meeting_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    audio_file TEXT,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

-- Meeting insights table (one-to-many with meetings)
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
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_history_timestamp ON transcription_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_meetings_start_time ON meetings(start_time);
CREATE INDEX IF NOT EXISTS idx_chunks_meeting_id ON meeting_chunks(meeting_id);
CREATE INDEX IF NOT EXISTS idx_insights_meeting_id ON meeting_insights(meeting_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_insights_unique
    ON meeting_insights(meeting_id, insight_type, COALESCE(custom_prompt, ''));
"""


class DatabaseManager:
    """Manages SQLite database for transcription and meeting storage."""
    
    def __init__(self, db_path: Optional[str] = None):
        """Initialize the database manager.
        
        Args:
            db_path: Path to the SQLite database file. Defaults to config.DATABASE_FILE.
        """
        self.db_path = db_path or getattr(config, 'DATABASE_FILE', 'openwhisper.db')
        self._local = threading.local()
        
        # Initialize the database
        self._init_database()
        
        # Run migration from JSON if needed
        self._migrate_from_json()
        
        logging.info(f"DatabaseManager initialized: {self.db_path}")
    
    @contextmanager
    def _get_connection(self):
        """Get a thread-local database connection.
        
        Yields:
            sqlite3.Connection: Database connection for the current thread.
        """
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0
            )
            self._local.connection.row_factory = sqlite3.Row
            # Enable foreign keys
            self._local.connection.execute("PRAGMA foreign_keys = ON")
        
        try:
            yield self._local.connection
        except Exception as e:
            self._local.connection.rollback()
            raise
    
    def _init_database(self) -> None:
        """Initialize database schema."""
        with self._get_connection() as conn:
            conn.executescript(SCHEMA_SQL)

            # Check and handle schema version
            cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
            row = cursor.fetchone()

            if row is None:
                # New database, set current version
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                current_version = row[0]
                if current_version < SCHEMA_VERSION:
                    self._run_migrations(conn, current_version)

            conn.commit()
            logging.info("Database schema initialized")

    def _run_migrations(self, conn, from_version: int) -> None:
        """Run database migrations from one version to another.

        Args:
            conn: Database connection.
            from_version: Current schema version in the database.
        """
        logging.info(f"Running database migrations from v{from_version} to v{SCHEMA_VERSION}")

        # Migration from v1 to v2: Add audio_file column to meetings table
        if from_version < 2:
            try:
                conn.execute("ALTER TABLE meetings ADD COLUMN audio_file TEXT DEFAULT NULL")
                logging.info("Migration v1->v2: Added audio_file column to meetings table")
            except sqlite3.OperationalError as e:
                # Column might already exist if migration was partially completed
                if "duplicate column name" not in str(e).lower():
                    raise
                logging.warning("Migration v1->v2: audio_file column already exists")

        # Migration from v2 to v3: Add meeting_insights table
        if from_version < 3:
            try:
                conn.execute("""
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
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_insights_meeting_id ON meeting_insights(meeting_id)"
                )
                conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_insights_unique
                    ON meeting_insights(meeting_id, insight_type, COALESCE(custom_prompt, ''))
                """)
                logging.info("Migration v2->v3: Added meeting_insights table")
            except sqlite3.OperationalError as e:
                if "already exists" not in str(e).lower():
                    raise
                logging.warning("Migration v2->v3: meeting_insights table already exists")

        # Migration from v3 to v4: Rename created_at to generated_at in meeting_insights
        if from_version < 4:
            try:
                # Check if we need to rename (created_at exists but generated_at doesn't)
                cursor = conn.execute("PRAGMA table_info(meeting_insights)")
                columns = [row[1] for row in cursor.fetchall()]

                if 'created_at' in columns and 'generated_at' not in columns:
                    conn.execute("""
                        ALTER TABLE meeting_insights
                        RENAME COLUMN created_at TO generated_at
                    """)
                    logging.info("Migration v3->v4: Renamed created_at to generated_at in meeting_insights")
                elif 'generated_at' in columns:
                    logging.info("Migration v3->v4: generated_at column already exists")
                else:
                    logging.warning("Migration v3->v4: Neither created_at nor generated_at found")
            except sqlite3.OperationalError as e:
                logging.error(f"Migration v3->v4 failed: {e}")
                raise

        # Update schema version
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        logging.info(f"Database migrated to schema version {SCHEMA_VERSION}")
    
    def _migrate_from_json(self) -> None:
        """Migrate existing JSON data to SQLite on first run."""
        history_file = getattr(config, 'HISTORY_FILE', 'transcription_history.json')
        meetings_file = getattr(config, 'MEETINGS_FILE', 'meetings.json')
        
        # Check if we've already migrated (data exists in DB)
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM transcription_history")
            history_count = cursor.fetchone()[0]
            cursor = conn.execute("SELECT COUNT(*) FROM meetings")
            meetings_count = cursor.fetchone()[0]
        
        # Migrate history if JSON exists and DB is empty
        if os.path.exists(history_file) and history_count == 0:
            self._migrate_history_from_json(history_file)
        
        # Migrate meetings if JSON exists and DB is empty
        if os.path.exists(meetings_file) and meetings_count == 0:
            self._migrate_meetings_from_json(meetings_file)
    
    def _migrate_history_from_json(self, json_path: str) -> None:
        """Migrate transcription history from JSON file.
        
        Args:
            json_path: Path to the JSON history file.
        """
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            entries = data.get('entries', [])
            if not entries:
                logging.info("No history entries to migrate")
                return
            
            with self._get_connection() as conn:
                for entry in entries:
                    conn.execute("""
                        INSERT OR IGNORE INTO transcription_history 
                        (id, text, timestamp, model, audio_file, transcription_time, audio_duration, file_size)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        entry.get('id'),
                        entry.get('text', ''),
                        entry.get('timestamp', ''),
                        entry.get('model', ''),
                        entry.get('audio_file'),
                        entry.get('transcription_time'),
                        entry.get('audio_duration'),
                        entry.get('file_size')
                    ))
                conn.commit()
            
            # Rename original file as backup
            backup_path = json_path + '.bak'
            os.rename(json_path, backup_path)
            logging.info(f"Migrated {len(entries)} history entries from JSON. Backup: {backup_path}")
            
        except Exception as e:
            logging.error(f"Failed to migrate history from JSON: {e}")
    
    def _migrate_meetings_from_json(self, json_path: str) -> None:
        """Migrate meetings from JSON file.
        
        Args:
            json_path: Path to the JSON meetings file.
        """
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            meetings = data.get('meetings', [])
            if not meetings:
                logging.info("No meetings to migrate")
                return
            
            with self._get_connection() as conn:
                for meeting in meetings:
                    meeting_id = meeting.get('id')
                    
                    # Insert meeting
                    conn.execute("""
                        INSERT OR IGNORE INTO meetings 
                        (id, title, start_time, end_time, duration_seconds, transcript, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        meeting_id,
                        meeting.get('title', ''),
                        meeting.get('start_time', ''),
                        meeting.get('end_time'),
                        meeting.get('duration_seconds', 0),
                        meeting.get('transcript', ''),
                        meeting.get('status', 'completed')
                    ))
                    
                    # Insert chunks
                    for chunk in meeting.get('chunks', []):
                        conn.execute("""
                            INSERT INTO meeting_chunks 
                            (meeting_id, chunk_index, text, timestamp, audio_file)
                            VALUES (?, ?, ?, ?, ?)
                        """, (
                            meeting_id,
                            chunk.get('index', 0),
                            chunk.get('text', ''),
                            chunk.get('timestamp', ''),
                            chunk.get('audio_file')
                        ))
                
                conn.commit()
            
            # Rename original file as backup
            backup_path = json_path + '.bak'
            os.rename(json_path, backup_path)
            logging.info(f"Migrated {len(meetings)} meetings from JSON. Backup: {backup_path}")
            
        except Exception as e:
            logging.error(f"Failed to migrate meetings from JSON: {e}")
    
    # -------------------------------------------------------------------------
    # Transcription History Methods
    # -------------------------------------------------------------------------
    
    def add_history_entry(
        self,
        entry_id: str,
        text: str,
        timestamp: str,
        model: str,
        audio_file: Optional[str] = None,
        transcription_time: Optional[float] = None,
        audio_duration: Optional[float] = None,
        file_size: Optional[int] = None
    ) -> None:
        """Add a transcription history entry.
        
        Args:
            entry_id: Unique entry ID.
            text: Transcribed text.
            timestamp: ISO format timestamp.
            model: Model used for transcription.
            audio_file: Optional path to saved audio file.
            transcription_time: Time taken to transcribe in seconds.
            audio_duration: Duration of audio in seconds.
            file_size: Size of audio file in bytes.
        """
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO transcription_history 
                (id, text, timestamp, model, audio_file, transcription_time, audio_duration, file_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (entry_id, text, timestamp, model, audio_file, transcription_time, audio_duration, file_size))
            conn.commit()
    
    def get_history_entries(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get transcription history entries.
        
        Args:
            limit: Optional maximum number of entries to return.
            
        Returns:
            List of history entry dictionaries (newest first).
        """
        with self._get_connection() as conn:
            query = "SELECT * FROM transcription_history ORDER BY timestamp DESC"
            if limit:
                query += f" LIMIT {limit}"
            
            cursor = conn.execute(query)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_history_entry_by_id(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific history entry by ID.
        
        Args:
            entry_id: The entry ID to find.
            
        Returns:
            Entry dictionary or None if not found.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM transcription_history WHERE id = ?", 
                (entry_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def delete_history_entry(self, entry_id: str) -> bool:
        """Delete a history entry.
        
        Args:
            entry_id: The entry ID to delete.
            
        Returns:
            True if deleted, False if not found.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM transcription_history WHERE id = ?", 
                (entry_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def clear_history(self) -> None:
        """Clear all history entries."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM transcription_history")
            conn.commit()
    
    def update_history_audio_file(self, audio_filename: str) -> None:
        """Clear audio_file reference for entries using a specific file.
        
        Args:
            audio_filename: The audio filename to clear references for.
        """
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE transcription_history SET audio_file = NULL WHERE audio_file = ?",
                (audio_filename,)
            )
            conn.commit()
    
    # -------------------------------------------------------------------------
    # Meeting Methods
    # -------------------------------------------------------------------------
    
    def create_meeting(self, meeting_id: str, title: str, start_time: str) -> None:
        """Create a new meeting.
        
        Args:
            meeting_id: Unique meeting ID.
            title: Meeting title.
            start_time: ISO format start time.
        """
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO meetings (id, title, start_time, status)
                VALUES (?, ?, ?, 'in_progress')
            """, (meeting_id, title, start_time))
            conn.commit()
    
    def add_meeting_chunk(
        self,
        meeting_id: str,
        chunk_index: int,
        text: str,
        timestamp: str,
        audio_file: Optional[str] = None
    ) -> None:
        """Add a chunk to a meeting.
        
        Args:
            meeting_id: Meeting ID.
            chunk_index: Index of this chunk.
            text: Transcribed text.
            timestamp: ISO format timestamp.
            audio_file: Optional path to audio chunk file.
        """
        with self._get_connection() as conn:
            # Insert chunk
            conn.execute("""
                INSERT INTO meeting_chunks (meeting_id, chunk_index, text, timestamp, audio_file)
                VALUES (?, ?, ?, ?, ?)
            """, (meeting_id, chunk_index, text, timestamp, audio_file))
            
            # Update meeting transcript
            cursor = conn.execute(
                "SELECT transcript FROM meetings WHERE id = ?",
                (meeting_id,)
            )
            row = cursor.fetchone()
            if row:
                current_transcript = row['transcript'] or ''
                new_transcript = (current_transcript + ' ' + text).strip() if current_transcript else text
                conn.execute(
                    "UPDATE meetings SET transcript = ? WHERE id = ?",
                    (new_transcript, meeting_id)
                )
            
            conn.commit()
    
    def end_meeting(self, meeting_id: str, end_time: str, duration_seconds: float,
                    audio_file: Optional[str] = None) -> None:
        """Mark a meeting as completed.

        Args:
            meeting_id: Meeting ID.
            end_time: ISO format end time.
            duration_seconds: Total meeting duration.
            audio_file: Optional path to the complete meeting audio recording.
        """
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE meetings
                SET end_time = ?, duration_seconds = ?, status = 'completed', audio_file = ?
                WHERE id = ?
            """, (end_time, duration_seconds, audio_file, meeting_id))
            conn.commit()
    
    def mark_meeting_interrupted(self, meeting_id: str, end_time: str, duration_seconds: float) -> None:
        """Mark a meeting as interrupted.
        
        Args:
            meeting_id: Meeting ID.
            end_time: ISO format end time.
            duration_seconds: Duration before interruption.
        """
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE meetings 
                SET end_time = ?, duration_seconds = ?, status = 'interrupted'
                WHERE id = ?
            """, (end_time, duration_seconds, meeting_id))
            conn.commit()
    
    def get_meeting(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        """Get a meeting by ID with its chunks.
        
        Args:
            meeting_id: Meeting ID.
            
        Returns:
            Meeting dictionary with chunks, or None if not found.
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
            row = cursor.fetchone()
            if not row:
                return None
            
            meeting = dict(row)
            
            # Get chunks
            cursor = conn.execute(
                "SELECT * FROM meeting_chunks WHERE meeting_id = ? ORDER BY chunk_index",
                (meeting_id,)
            )
            meeting['chunks'] = [dict(chunk) for chunk in cursor.fetchall()]
            
            return meeting
    
    def get_all_meetings(self) -> List[Dict[str, Any]]:
        """Get all meetings (newest first).
        
        Returns:
            List of meeting dictionaries with chunks.
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM meetings ORDER BY start_time DESC")
            meetings = []
            for row in cursor.fetchall():
                meeting = dict(row)
                # Get chunks for this meeting
                chunks_cursor = conn.execute(
                    "SELECT * FROM meeting_chunks WHERE meeting_id = ? ORDER BY chunk_index",
                    (meeting['id'],)
                )
                meeting['chunks'] = [dict(chunk) for chunk in chunks_cursor.fetchall()]
                meetings.append(meeting)
            return meetings
    
    def get_in_progress_meetings(self) -> List[Dict[str, Any]]:
        """Get meetings that are still in progress.
        
        Returns:
            List of in-progress meeting dictionaries.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM meetings WHERE status = 'in_progress'"
            )
            return [dict(row) for row in cursor.fetchall()]

    
    def delete_meeting(self, meeting_id: str) -> bool:
        """Delete a meeting and its chunks.
        
        Args:
            meeting_id: Meeting ID to delete.
            
        Returns:
            True if deleted, False if not found.
        """
        with self._get_connection() as conn:
            # Chunks are deleted via CASCADE
            cursor = conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            conn.commit()
            return cursor.rowcount > 0
    
    def update_meeting_title(self, meeting_id: str, title: str) -> bool:
        """Update a meeting's title.
        
        Args:
            meeting_id: Meeting ID.
            title: New title.
            
        Returns:
            True if updated, False if not found.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE meetings SET title = ? WHERE id = ?",
                (title, meeting_id)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def get_meeting_chunk_count(self, meeting_id: str) -> int:
        """Get the number of chunks for a meeting.

        Args:
            meeting_id: Meeting ID.

        Returns:
            Number of chunks.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM meeting_chunks WHERE meeting_id = ?",
                (meeting_id,)
            )
            return cursor.fetchone()[0]

    def get_meeting_audio_file(self, meeting_id: str) -> Optional[str]:
        """Get the audio file path for a meeting.

        Args:
            meeting_id: Meeting ID.

        Returns:
            Audio file path, or None if not found or no audio file.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT audio_file FROM meetings WHERE id = ?",
                (meeting_id,)
            )
            row = cursor.fetchone()
            return row['audio_file'] if row else None

    # -------------------------------------------------------------------------
    # Meeting Insights Methods
    # -------------------------------------------------------------------------

    def save_insight(
        self,
        meeting_id: str,
        insight_type: str,
        content: str,
        custom_prompt: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None
    ) -> int:
        """Save or update an insight for a meeting.

        Uses upsert logic - replaces existing insight if one exists for the
        same meeting_id, insight_type, and custom_prompt combination.

        Args:
            meeting_id: Meeting ID.
            insight_type: Type of insight ('summary', 'action_items', 'custom').
            content: The generated insight content.
            custom_prompt: Custom prompt (only for 'custom' type).
            provider: LLM provider used ('openai', 'openrouter').
            model: Model name used.

        Returns:
            The row ID of the saved insight.
        """
        generated_at = datetime.now().isoformat()

        with self._get_connection() as conn:
            # Use INSERT OR REPLACE with the unique constraint
            # First, try to find existing insight
            cursor = conn.execute("""
                SELECT id FROM meeting_insights
                WHERE meeting_id = ? AND insight_type = ?
                AND COALESCE(custom_prompt, '') = COALESCE(?, '')
            """, (meeting_id, insight_type, custom_prompt))
            existing = cursor.fetchone()

            if existing:
                # Update existing
                conn.execute("""
                    UPDATE meeting_insights
                    SET content = ?, generated_at = ?, provider = ?, model = ?
                    WHERE id = ?
                """, (content, generated_at, provider, model, existing['id']))
                insight_id = existing['id']
            else:
                # Insert new
                cursor = conn.execute("""
                    INSERT INTO meeting_insights
                    (meeting_id, insight_type, content, custom_prompt, generated_at, provider, model)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (meeting_id, insight_type, content, custom_prompt, generated_at, provider, model))
                insight_id = cursor.lastrowid

            conn.commit()
            return insight_id

    def get_insight(
        self,
        meeting_id: str,
        insight_type: str,
        custom_prompt: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Get a specific insight for a meeting.

        Args:
            meeting_id: Meeting ID.
            insight_type: Type of insight ('summary', 'action_items', 'custom').
            custom_prompt: Custom prompt (only for 'custom' type).

        Returns:
            Insight dictionary or None if not found.
        """
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM meeting_insights
                WHERE meeting_id = ? AND insight_type = ?
                AND COALESCE(custom_prompt, '') = COALESCE(?, '')
            """, (meeting_id, insight_type, custom_prompt))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_insights(self, meeting_id: str) -> List[Dict[str, Any]]:
        """Get all insights for a meeting.

        Args:
            meeting_id: Meeting ID.

        Returns:
            List of insight dictionaries.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM meeting_insights WHERE meeting_id = ? ORDER BY generated_at DESC",
                (meeting_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def delete_insight(self, insight_id: int) -> bool:
        """Delete an insight by ID.

        Args:
            insight_id: The insight ID to delete.

        Returns:
            True if deleted, False if not found.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM meeting_insights WHERE id = ?",
                (insight_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def has_insights(self, meeting_id: str) -> bool:
        """Check if a meeting has any saved insights.

        Args:
            meeting_id: Meeting ID.

        Returns:
            True if the meeting has at least one saved insight.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM meeting_insights WHERE meeting_id = ? LIMIT 1",
                (meeting_id,)
            )
            return cursor.fetchone() is not None

    def close(self) -> None:
        """Close the database connection for the current thread."""
        if hasattr(self._local, 'connection') and self._local.connection is not None:
            self._local.connection.close()
            self._local.connection = None


# Global database manager instance
db = DatabaseManager()
