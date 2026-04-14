"""
Meeting Storage for persistent meeting data and auto-save.
Manages meeting metadata, transcriptions, and audio files.
"""
import logging
import os
import shutil
import wave
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from config import config
from services.database import db
from services.models import Meeting as MeetingEntry

if TYPE_CHECKING:
    from services.recorder import AudioRecorder


class MeetingStorage:
    """Manages persistent storage for meeting data."""

    def __init__(self, audio_folder: str = "meeting_audio",
                 recordings_folder: str = None):
        """Initialize meeting storage.

        Args:
            audio_folder: Path to folder for meeting audio chunk files
            recordings_folder: Path to folder for complete meeting recordings
        """
        self.audio_folder = audio_folder
        self.recordings_folder = recordings_folder or config.MEETING_RECORDINGS_FOLDER
        self._current_meeting_id: Optional[str] = None

        # Ensure audio folders exist
        os.makedirs(self.audio_folder, exist_ok=True)
        os.makedirs(self.recordings_folder, exist_ok=True)

        # Check for interrupted meetings
        self._check_interrupted_meetings()

        logging.info(f"MeetingStorage initialized (audio: {self.audio_folder}, recordings: {self.recordings_folder})")
    
    def _check_interrupted_meetings(self) -> None:
        """Check for meetings that were interrupted (app crash during recording)."""
        in_progress = db.get_in_progress_meetings()
        for meeting in in_progress:
            logging.warning(f"Found interrupted meeting: {meeting.id[:8]}... ({meeting.title})")
            db.mark_meeting_interrupted(
                meeting.id,
                datetime.now().isoformat(),
                meeting.duration_seconds or 0,
            )
            db.reset_processing_chunks_to_pending(meeting.id)
    
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

    def register_spool_chunk(
        self,
        meeting_id: str,
        chunk_index: int,
        audio_file: str,
        start_offset_sec: float,
        end_offset_sec: float,
    ) -> bool:
        """Register a durable audio chunk file for later transcription."""
        if self._current_meeting_id is None or self._current_meeting_id != meeting_id:
            logging.warning("No active meeting to register spool chunk for")
            return False

        try:
            db.register_spool_chunk(
                meeting_id=meeting_id,
                chunk_index=chunk_index,
                audio_file=audio_file,
                start_offset_sec=start_offset_sec,
                end_offset_sec=end_offset_sec,
            )
            return True
        except Exception as e:
            logging.error(f"Failed to register spool chunk: {e}")
            return False
    
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
    
    def end_meeting(self, duration_seconds: float,
                    audio_file: Optional[str] = None) -> Optional[MeetingEntry]:
        """End the current meeting.

        Args:
            duration_seconds: Total meeting duration
            audio_file: Optional path to the complete meeting audio recording

        Returns:
            The completed MeetingEntry, or None if no active meeting
        """
        if self._current_meeting_id is None:
            logging.warning("No active meeting to end")
            return None

        meeting_id = self._current_meeting_id

        # Update in database (with audio_file if provided)
        db.end_meeting(meeting_id, datetime.now().isoformat(), duration_seconds, audio_file)

        self._current_meeting_id = None

        # Get the completed meeting
        meeting = self.get_meeting(meeting_id)

        if meeting:
            audio_info = f", audio: {audio_file}" if audio_file else ""
            logging.info(f"Ended meeting: {meeting.id[:8]}... (duration: {meeting.formatted_duration}{audio_info})")

        return meeting
    
    def save_complete_recording(self, meeting_id: str, recorder: 'AudioRecorder',
                                 max_recordings: int = None) -> Optional[str]:
        """Save the complete recording from the recorder to a WAV file.

        Args:
            meeting_id: Meeting ID for the filename
            recorder: AudioRecorder instance with recorded frames
            max_recordings: Maximum recordings to keep (for rotation). Uses config default if None.

        Returns:
            Relative path to saved file, or None if failed or no data
        """
        from services.settings import settings_manager

        if not recorder or not recorder.has_recording_data():
            logging.warning("No recording data to save for meeting")
            return None

        # Get max recordings from settings if not provided
        if max_recordings is None:
            settings = settings_manager.load_meeting_recording_settings()
            max_recordings = settings.get('max_recordings', config.MAX_MEETING_RECORDINGS)

        try:
            # Rotate old recordings if needed
            self._rotate_recordings(max_recordings)

            # Generate filename: meeting_YYYYMMDD_HHMMSS_{id[:8]}.wav
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"meeting_{timestamp}_{meeting_id[:8]}.wav"
            file_path = os.path.join(self.recordings_folder, filename)

            # Check disk space (warn if less than 2x estimated file size)
            estimated_size = self._estimate_recording_size(recorder)
            self._check_disk_space(file_path, estimated_size * 2)

            # Get a snapshot of frames while holding the callback lock
            with recorder._callback_lock:
                frames_to_write = list(recorder.frames)

            if not frames_to_write:
                logging.warning("No frames to write for meeting recording")
                return None

            # Write WAV file
            with wave.open(file_path, 'wb') as wf:
                wf.setnchannels(recorder.channels)
                wf.setsampwidth(np.dtype(recorder.dtype).itemsize)
                wf.setframerate(recorder.rate)
                wf.writeframes(b''.join(frames_to_write))

            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            logging.info(f"Saved complete meeting recording: {filename} ({file_size_mb:.1f} MB)")

            return filename

        except Exception as e:
            logging.error(f"Failed to save complete meeting recording: {e}")
            return None

    def _estimate_recording_size(self, recorder: 'AudioRecorder') -> int:
        """Estimate the size of the recording in bytes.

        Args:
            recorder: AudioRecorder instance

        Returns:
            Estimated size in bytes
        """
        if not recorder.frames:
            return 0
        total_bytes = sum(len(frame) for frame in recorder.frames)
        # Add WAV header overhead (~44 bytes)
        return total_bytes + 44

    def _check_disk_space(self, file_path: str, required_bytes: int) -> None:
        """Check if there's enough disk space for the file.

        Args:
            file_path: Path where the file will be saved
            required_bytes: Required space in bytes

        Logs a warning if space is low but does not raise an exception.
        """
        try:
            folder = os.path.dirname(file_path) or '.'
            free_bytes = shutil.disk_usage(folder).free
            if free_bytes < required_bytes:
                required_mb = required_bytes / (1024 * 1024)
                free_mb = free_bytes / (1024 * 1024)
                logging.warning(f"Low disk space for meeting recording. "
                               f"Required: ~{required_mb:.1f} MB, Available: {free_mb:.1f} MB")
        except Exception as e:
            logging.debug(f"Could not check disk space: {e}")

    def _rotate_recordings(self, max_recordings: int) -> None:
        """Delete oldest recordings if exceeding the maximum count.

        Args:
            max_recordings: Maximum number of recordings to keep
        """
        try:
            # Get all WAV files in the recordings folder
            recordings = []
            for filename in os.listdir(self.recordings_folder):
                if filename.startswith('meeting_') and filename.endswith('.wav'):
                    file_path = os.path.join(self.recordings_folder, filename)
                    recordings.append((file_path, os.path.getmtime(file_path)))

            # Sort by modification time (oldest first)
            recordings.sort(key=lambda x: x[1])

            # Delete oldest if we're at or over the limit (leave room for new one)
            while len(recordings) >= max_recordings:
                oldest_path, _ = recordings.pop(0)
                try:
                    os.remove(oldest_path)
                    logging.info(f"Rotated out old meeting recording: {os.path.basename(oldest_path)}")
                except Exception as e:
                    logging.warning(f"Failed to delete old recording {oldest_path}: {e}")

        except Exception as e:
            logging.warning(f"Error during recording rotation: {e}")

    def get_meeting(self, meeting_id: str) -> Optional[MeetingEntry]:
        """Get a meeting by ID.

        Args:
            meeting_id: Meeting ID

        Returns:
            The MeetingEntry, or None if not found
        """
        return db.get_meeting(meeting_id)
    
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
        return db.get_all_meetings()
    
    def get_meetings_for_display(self) -> List[Dict[str, str]]:
        """Get meeting data formatted for UI display.

        Returns:
            List of meeting dicts with id, title, date, duration, preview, status
        """
        meetings = self.get_all_meetings()
        return [
            {
                'id': m.id,
                'title': m.title,
                'date': m.formatted_start_time,
                'duration': m.formatted_duration,
                'preview': m.preview_text,
                'status': m.status,
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
        # Get meeting info first for logging and audio file path
        meeting = self.get_meeting(meeting_id)
        if not meeting:
            return False

        # Delete audio chunk folder if it exists
        audio_folder = os.path.join(self.audio_folder, meeting_id[:8])
        if os.path.exists(audio_folder):
            try:
                shutil.rmtree(audio_folder)
                logging.info(f"Deleted audio chunk folder for meeting {meeting_id[:8]}...")
            except Exception as e:
                logging.warning(f"Failed to delete audio chunk folder: {e}")

        # Delete complete recording if it exists
        if meeting.audio_file:
            recording_path = os.path.join(self.recordings_folder, meeting.audio_file)
            if os.path.exists(recording_path):
                try:
                    os.remove(recording_path)
                    logging.info(f"Deleted complete recording for meeting {meeting_id[:8]}...")
                except Exception as e:
                    logging.warning(f"Failed to delete complete recording: {e}")

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
