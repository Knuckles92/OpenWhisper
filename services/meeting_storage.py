"""
Meeting Storage for persistent meeting data and auto-save.
Manages meeting metadata, transcriptions, and audio files.
"""
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict, field
import uuid

from config import config
from services.database import db


@dataclass
class MeetingChunk:
    """Represents a single transcription chunk from a meeting."""
    index: int
    text: str
    timestamp: str  # ISO format
    audio_file: Optional[str] = None  # Relative path to audio chunk if saved


@dataclass
class MeetingEntry:
    """Represents a complete meeting record."""
    id: str
    title: str
    start_time: str  # ISO format
    end_time: Optional[str]  # ISO format, None if still in progress
    duration_seconds: float
    transcript: str  # Full accumulated transcript
    chunks: List[MeetingChunk] = field(default_factory=list)
    audio_files: List[str] = field(default_factory=list)  # Paths to audio chunk files
    status: str = "in_progress"  # "in_progress", "completed", "interrupted"
    
    @classmethod
    def create(cls, title: str = "") -> 'MeetingEntry':
        """Create a new meeting entry with auto-generated id and start time."""
        return cls(
            id=str(uuid.uuid4()),
            title=title or f"Meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            start_time=datetime.now().isoformat(),
            end_time=None,
            duration_seconds=0.0,
            transcript="",
            chunks=[],
            audio_files=[],
            status="in_progress"
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        # Convert chunks to dicts
        data['chunks'] = [asdict(c) if hasattr(c, '__dict__') else c for c in self.chunks]
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MeetingEntry':
        """Create from dictionary."""
        # Convert chunk dicts back to MeetingChunk objects
        chunks_data = data.pop('chunks', [])
        chunks = []
        for chunk_data in chunks_data:
            if isinstance(chunk_data, dict):
                # Handle database row format (has 'chunk_index' instead of 'index')
                if 'chunk_index' in chunk_data and 'index' not in chunk_data:
                    chunk_data['index'] = chunk_data.pop('chunk_index')
                # Remove extra fields from database
                chunk_data.pop('id', None)
                chunk_data.pop('meeting_id', None)
                chunks.append(MeetingChunk(**chunk_data))
            else:
                chunks.append(chunk_data)
        
        # Remove fields not in dataclass (from database)
        data.pop('audio_files', None)  # We'll compute this from chunks
        
        entry = cls(chunks=chunks, **data)
        # Rebuild audio_files from chunks
        entry.audio_files = [c.audio_file for c in chunks if c.audio_file]
        return entry
    
    @property
    def formatted_start_time(self) -> str:
        """Get human-readable start time."""
        try:
            dt = datetime.fromisoformat(self.start_time)
            return dt.strftime("%b %d, %Y %I:%M %p")
        except Exception:
            return self.start_time
    
    @property
    def formatted_duration(self) -> str:
        """Get human-readable duration."""
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
        """Get truncated preview of transcript."""
        max_len = 100
        if len(self.transcript) <= max_len:
            return self.transcript
        return self.transcript[:max_len].rsplit(' ', 1)[0] + "..."
    
    def add_chunk(self, text: str, audio_file: Optional[str] = None):
        """Add a transcription chunk to the meeting.
        
        Args:
            text: Transcribed text
            audio_file: Optional path to audio chunk file
        """
        chunk = MeetingChunk(
            index=len(self.chunks),
            text=text,
            timestamp=datetime.now().isoformat(),
            audio_file=audio_file
        )
        self.chunks.append(chunk)
        
        # Update full transcript
        if self.transcript:
            self.transcript += " " + text
        else:
            self.transcript = text
        
        if audio_file:
            self.audio_files.append(audio_file)
    
    def complete(self, duration_seconds: float):
        """Mark the meeting as completed.
        
        Args:
            duration_seconds: Total meeting duration in seconds
        """
        self.end_time = datetime.now().isoformat()
        self.duration_seconds = duration_seconds
        self.status = "completed"
    
    def mark_interrupted(self, duration_seconds: float):
        """Mark the meeting as interrupted (e.g., crash recovery).
        
        Args:
            duration_seconds: Duration before interruption
        """
        self.end_time = datetime.now().isoformat()
        self.duration_seconds = duration_seconds
        self.status = "interrupted"


class MeetingStorage:
    """Manages persistent storage for meeting data."""
    
    def __init__(self, audio_folder: str = "meeting_audio"):
        """Initialize meeting storage.
        
        Args:
            audio_folder: Path to folder for meeting audio files
        """
        self.audio_folder = audio_folder
        self._current_meeting_id: Optional[str] = None
        
        # Ensure audio folder exists
        os.makedirs(self.audio_folder, exist_ok=True)
        
        # Check for interrupted meetings
        self._check_interrupted_meetings()
        
        logging.info(f"MeetingStorage initialized (audio: {self.audio_folder})")
    
    def _check_interrupted_meetings(self) -> None:
        """Check for meetings that were interrupted (app crash during recording)."""
        in_progress = db.get_in_progress_meetings()
        for meeting_data in in_progress:
            logging.warning(f"Found interrupted meeting: {meeting_data['id'][:8]}... ({meeting_data['title']})")
            db.mark_meeting_interrupted(
                meeting_data['id'],
                datetime.now().isoformat(),
                meeting_data.get('duration_seconds', 0)
            )
    
    def start_meeting(self, title: str = "") -> MeetingEntry:
        """Start a new meeting.
        
        Args:
            title: Optional meeting title
            
        Returns:
            The created MeetingEntry
        """
        meeting = MeetingEntry.create(title)
        
        # Save to database
        db.create_meeting(meeting.id, meeting.title, meeting.start_time)
        self._current_meeting_id = meeting.id
        
        logging.info(f"Started meeting: {meeting.id[:8]}... ({meeting.title})")
        return meeting
    
    def add_chunk(self, text: str, audio_data: Optional[bytes] = None) -> bool:
        """Add a transcription chunk to the current meeting.
        
        Args:
            text: Transcribed text
            audio_data: Optional raw audio data to save
            
        Returns:
            True if successful, False if no active meeting
        """
        if self._current_meeting_id is None:
            logging.warning("No active meeting to add chunk to")
            return False
        
        # Get current chunk count for index
        chunk_index = db.get_meeting_chunk_count(self._current_meeting_id)
        
        # Save audio chunk if provided
        audio_file = None
        if audio_data:
            audio_file = self._save_audio_chunk(self._current_meeting_id, chunk_index, audio_data)
        
        # Add chunk to database
        db.add_meeting_chunk(
            meeting_id=self._current_meeting_id,
            chunk_index=chunk_index,
            text=text,
            timestamp=datetime.now().isoformat(),
            audio_file=audio_file
        )
        
        logging.debug(f"Added chunk to meeting {self._current_meeting_id[:8]}...: {len(text)} chars")
        return True
    
    def _save_audio_chunk(self, meeting_id: str, chunk_index: int, 
                          audio_data: bytes) -> Optional[str]:
        """Save an audio chunk to disk.
        
        Args:
            meeting_id: Meeting ID
            chunk_index: Index of this chunk
            audio_data: Raw audio bytes
            
        Returns:
            Relative path to saved file, or None if failed
        """
        try:
            # Create meeting-specific folder
            meeting_folder = os.path.join(self.audio_folder, meeting_id[:8])
            os.makedirs(meeting_folder, exist_ok=True)
            
            # Save chunk
            filename = f"chunk_{chunk_index:04d}.wav"
            file_path = os.path.join(meeting_folder, filename)
            
            with open(file_path, 'wb') as f:
                f.write(audio_data)
            
            # Return relative path
            return os.path.join(meeting_id[:8], filename)
            
        except Exception as e:
            logging.error(f"Failed to save audio chunk: {e}")
            return None
    
    def end_meeting(self, duration_seconds: float) -> Optional[MeetingEntry]:
        """End the current meeting.
        
        Args:
            duration_seconds: Total meeting duration
            
        Returns:
            The completed MeetingEntry, or None if no active meeting
        """
        if self._current_meeting_id is None:
            logging.warning("No active meeting to end")
            return None
        
        meeting_id = self._current_meeting_id
        
        # Update in database
        db.end_meeting(meeting_id, datetime.now().isoformat(), duration_seconds)
        
        self._current_meeting_id = None
        
        # Get the completed meeting
        meeting = self.get_meeting(meeting_id)
        
        if meeting:
            logging.info(f"Ended meeting: {meeting.id[:8]}... (duration: {meeting.formatted_duration})")
        
        return meeting
    
    def get_meeting(self, meeting_id: str) -> Optional[MeetingEntry]:
        """Get a meeting by ID.
        
        Args:
            meeting_id: Meeting ID
            
        Returns:
            The MeetingEntry, or None if not found
        """
        data = db.get_meeting(meeting_id)
        return MeetingEntry.from_dict(data) if data else None
    
    def get_current_meeting(self) -> Optional[MeetingEntry]:
        """Get the current active meeting.
        
        Returns:
            The current MeetingEntry, or None if no active meeting
        """
        if self._current_meeting_id is None:
            return None
        return self.get_meeting(self._current_meeting_id)
    
    def get_all_meetings(self) -> List[MeetingEntry]:
        """Get all meetings sorted by start time (newest first).
        
        Returns:
            List of MeetingEntry objects
        """
        meetings_data = db.get_all_meetings()
        return [MeetingEntry.from_dict(data) for data in meetings_data]
    
    def get_meetings_for_display(self) -> List[Dict[str, str]]:
        """Get meeting data formatted for UI display.
        
        Returns:
            List of meeting dicts with id, title, date, duration, preview
        """
        meetings = self.get_all_meetings()
        return [
            {
                'id': m.id,
                'title': m.title,
                'date': m.formatted_start_time,
                'duration': m.formatted_duration,
                'preview': m.preview_text,
                'status': m.status
            }
            for m in meetings
        ]
    
    def delete_meeting(self, meeting_id: str) -> bool:
        """Delete a meeting and its associated audio files.
        
        Args:
            meeting_id: Meeting ID to delete
            
        Returns:
            True if deleted, False if not found
        """
        # Get meeting info first for logging
        meeting = self.get_meeting(meeting_id)
        if not meeting:
            return False
        
        # Delete audio folder if it exists
        audio_folder = os.path.join(self.audio_folder, meeting_id[:8])
        if os.path.exists(audio_folder):
            try:
                shutil.rmtree(audio_folder)
                logging.info(f"Deleted audio folder for meeting {meeting_id[:8]}...")
            except Exception as e:
                logging.warning(f"Failed to delete audio folder: {e}")
        
        # Delete from database
        result = db.delete_meeting(meeting_id)
        
        if result:
            logging.info(f"Deleted meeting: {meeting_id[:8]}... ({meeting.title})")
        
        return result
    
    def update_meeting_title(self, meeting_id: str, title: str) -> bool:
        """Update a meeting's title.
        
        Args:
            meeting_id: Meeting ID
            title: New title
            
        Returns:
            True if updated, False if not found
        """
        return db.update_meeting_title(meeting_id, title)


# Global meeting storage instance
meeting_storage = MeetingStorage()
