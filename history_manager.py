"""
History management for transcriptions and recordings.
Stores transcription history and manages the last N audio recordings.
"""
import json
import os
import shutil
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
import uuid


@dataclass
class HistoryEntry:
    """Represents a single transcription history entry."""
    id: str
    text: str
    timestamp: str
    model: str
    audio_file: Optional[str] = None  # Relative path to saved recording if available
    transcription_time: Optional[float] = None  # Time taken to transcribe in seconds
    audio_duration: Optional[float] = None  # Duration of the audio in seconds
    file_size: Optional[int] = None  # Size of the audio file in bytes

    @classmethod
    def create(
        cls,
        text: str,
        model: str,
        audio_file: Optional[str] = None,
        transcription_time: Optional[float] = None,
        audio_duration: Optional[float] = None,
        file_size: Optional[int] = None
    ) -> 'HistoryEntry':
        """Create a new history entry with auto-generated id and timestamp."""
        return cls(
            id=str(uuid.uuid4()),
            text=text,
            timestamp=datetime.now().isoformat(),
            model=model,
            audio_file=audio_file,
            transcription_time=transcription_time,
            audio_duration=audio_duration,
            file_size=file_size
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'HistoryEntry':
        """Create from dictionary."""
        return cls(**data)
    
    @property
    def formatted_timestamp(self) -> str:
        """Get human-readable timestamp."""
        try:
            dt = datetime.fromisoformat(self.timestamp)
            return dt.strftime("%b %d, %Y %I:%M %p")
        except Exception:
            return self.timestamp
    
    @property
    def preview_text(self) -> str:
        """Get truncated preview of transcription text."""
        max_len = 100
        if len(self.text) <= max_len:
            return self.text
        return self.text[:max_len].rsplit(' ', 1)[0] + "..."


@dataclass
class RecordingInfo:
    """Represents a saved audio recording."""
    filename: str
    timestamp: str
    file_path: str
    size_bytes: int
    
    @property
    def formatted_timestamp(self) -> str:
        """Get human-readable timestamp."""
        try:
            dt = datetime.fromisoformat(self.timestamp)
            return dt.strftime("%b %d, %Y %I:%M %p")
        except Exception:
            return self.timestamp
    
    @property
    def formatted_size(self) -> str:
        """Get human-readable file size."""
        size = self.size_bytes
        for unit in ['B', 'KB', 'MB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"


class HistoryManager:
    """Manages transcription history and saved recordings."""
    
    def __init__(self, history_file: str = None, recordings_folder: str = None, max_recordings: int = 3):
        """Initialize the history manager.
        
        Args:
            history_file: Path to the JSON history file.
            recordings_folder: Path to folder for saved recordings.
            max_recordings: Maximum number of recordings to keep.
        """
        # Import config here to avoid circular imports
        from config import config
        
        self.history_file = history_file or config.HISTORY_FILE
        self.recordings_folder = recordings_folder or config.RECORDINGS_FOLDER
        self.max_recordings = max_recordings or config.MAX_SAVED_RECORDINGS
        
        self._lock = threading.Lock()
        self._history: List[HistoryEntry] = []
        
        # Ensure recordings folder exists
        os.makedirs(self.recordings_folder, exist_ok=True)
        
        # Load existing history
        self._load_history()
    
    def _load_history(self) -> None:
        """Load history from file."""
        with self._lock:
            try:
                if os.path.exists(self.history_file):
                    with open(self.history_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self._history = [
                            HistoryEntry.from_dict(entry) 
                            for entry in data.get('entries', [])
                        ]
                    logging.info(f"Loaded {len(self._history)} history entries")
            except Exception as e:
                logging.error(f"Failed to load history: {e}")
                self._history = []
    
    def _save_history(self) -> None:
        """Save history to file."""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'entries': [entry.to_dict() for entry in self._history]
                }, f, indent=2, ensure_ascii=False)
            logging.debug("History saved successfully")
        except Exception as e:
            logging.error(f"Failed to save history: {e}")
    
    def add_entry(
        self,
        text: str,
        model: str,
        source_audio_file: Optional[str] = None,
        transcription_time: Optional[float] = None,
        audio_duration: Optional[float] = None,
        file_size: Optional[int] = None
    ) -> HistoryEntry:
        """Add a new transcription to history.

        Args:
            text: The transcribed text.
            model: The model used for transcription (display name or internal value).
            source_audio_file: Optional path to source audio file to save.
            transcription_time: Time taken to transcribe in seconds.
            audio_duration: Duration of the audio in seconds.
            file_size: Size of the audio file in bytes.

        Returns:
            The created HistoryEntry.
        """
        saved_audio_path = None

        # Save the audio recording if provided
        if source_audio_file and os.path.exists(source_audio_file):
            saved_audio_path = self._save_recording(source_audio_file)

        # Create and add the entry
        entry = HistoryEntry.create(
            text=text,
            model=model,
            audio_file=saved_audio_path,
            transcription_time=transcription_time,
            audio_duration=audio_duration,
            file_size=file_size
        )
        
        with self._lock:
            # Add to beginning (newest first)
            self._history.insert(0, entry)
            self._save_history()
        
        logging.info(f"Added history entry: {entry.id[:8]}...")
        return entry
    
    def _save_recording(self, source_file: str) -> Optional[str]:
        """Save a recording to the recordings folder with rotation.
        
        Args:
            source_file: Path to the source audio file.
            
        Returns:
            Relative path to saved recording, or None if failed.
        """
        try:
            # Generate filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"recording_{timestamp}.wav"
            dest_path = os.path.join(self.recordings_folder, filename)
            
            # Copy the file
            shutil.copy2(source_file, dest_path)
            logging.info(f"Saved recording: {filename}")
            
            # Rotate old recordings
            self._rotate_recordings()
            
            return filename
            
        except Exception as e:
            logging.error(f"Failed to save recording: {e}")
            return None
    
    def _rotate_recordings(self) -> None:
        """Remove oldest recordings if we exceed max_recordings."""
        try:
            recordings = self.get_recordings()
            
            if len(recordings) > self.max_recordings:
                # Sort by timestamp (oldest first)
                recordings.sort(key=lambda r: r.timestamp)
                
                # Remove oldest recordings
                to_remove = recordings[:-self.max_recordings]
                for rec in to_remove:
                    try:
                        os.remove(rec.file_path)
                        logging.info(f"Removed old recording: {rec.filename}")
                        
                        # Clear audio_file reference in history entries
                        with self._lock:
                            for entry in self._history:
                                if entry.audio_file == rec.filename:
                                    entry.audio_file = None
                            self._save_history()
                            
                    except Exception as e:
                        logging.error(f"Failed to remove recording {rec.filename}: {e}")
                        
        except Exception as e:
            logging.error(f"Failed to rotate recordings: {e}")
    
    def get_history(self, limit: Optional[int] = None) -> List[HistoryEntry]:
        """Get transcription history entries.
        
        Args:
            limit: Optional maximum number of entries to return.
            
        Returns:
            List of HistoryEntry objects (newest first).
        """
        with self._lock:
            if limit is not None:
                return self._history[:limit]
            return list(self._history)
    
    def get_recordings(self) -> List[RecordingInfo]:
        """Get list of saved recordings.
        
        Returns:
            List of RecordingInfo objects (newest first).
        """
        recordings = []
        
        try:
            if not os.path.exists(self.recordings_folder):
                return recordings
            
            for filename in os.listdir(self.recordings_folder):
                if filename.endswith('.wav'):
                    file_path = os.path.join(self.recordings_folder, filename)
                    
                    # Get file info
                    stat = os.stat(file_path)
                    
                    # Extract timestamp from filename (recording_YYYYMMDD_HHMMSS.wav)
                    try:
                        parts = filename.replace('recording_', '').replace('.wav', '')
                        dt = datetime.strptime(parts, "%Y%m%d_%H%M%S")
                        timestamp = dt.isoformat()
                    except Exception:
                        # Fallback to file modification time
                        timestamp = datetime.fromtimestamp(stat.st_mtime).isoformat()
                    
                    recordings.append(RecordingInfo(
                        filename=filename,
                        timestamp=timestamp,
                        file_path=file_path,
                        size_bytes=stat.st_size
                    ))
            
            # Sort by timestamp (newest first)
            recordings.sort(key=lambda r: r.timestamp, reverse=True)
            
        except Exception as e:
            logging.error(f"Failed to get recordings: {e}")
        
        return recordings
    
    def get_entry_by_id(self, entry_id: str) -> Optional[HistoryEntry]:
        """Get a specific history entry by ID.
        
        Args:
            entry_id: The entry ID to find.
            
        Returns:
            The HistoryEntry or None if not found.
        """
        with self._lock:
            for entry in self._history:
                if entry.id == entry_id:
                    return entry
        return None
    
    def delete_entry(self, entry_id: str) -> bool:
        """Delete a history entry.
        
        Args:
            entry_id: The entry ID to delete.
            
        Returns:
            True if deleted, False if not found.
        """
        with self._lock:
            for i, entry in enumerate(self._history):
                if entry.id == entry_id:
                    # Don't delete the associated audio file - it may be used by other entries
                    del self._history[i]
                    self._save_history()
                    logging.info(f"Deleted history entry: {entry_id[:8]}...")
                    return True
        return False
    
    def clear_history(self) -> None:
        """Clear all history entries (keeps recordings)."""
        with self._lock:
            self._history = []
            self._save_history()
        logging.info("History cleared")
    
    def get_recording_path(self, filename: str) -> Optional[str]:
        """Get full path to a recording by filename.
        
        Args:
            filename: The recording filename.
            
        Returns:
            Full path to the file, or None if not found.
        """
        if not filename:
            return None
            
        file_path = os.path.join(self.recordings_folder, filename)
        if os.path.exists(file_path):
            return file_path
        return None


# Global history manager instance
history_manager = HistoryManager()

