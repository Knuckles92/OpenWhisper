"""
History management for transcriptions and recordings.
Stores transcription history and manages the last N audio recordings.
"""
import logging
import os
import shutil
from datetime import datetime
from typing import List, Optional
from dataclasses import dataclass

from config import config
from services.database import db
from services.format_utils import format_file_size, format_timestamp
from services.models import TranscriptionHistory as HistoryEntry
from services.settings import resolve_max_saved_recordings

logger = logging.getLogger(__name__)

# Sentinel so callers can pass ``max_recordings=None`` for keep-all.
_UNSET = object()


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
        return format_timestamp(self.timestamp)

    @property
    def formatted_size(self) -> str:
        """Get human-readable file size."""
        return format_file_size(self.size_bytes)


class HistoryManager:
    """Manages transcription history and saved recordings."""

    def __init__(
        self,
        recordings_folder: str = None,
        max_recordings: Optional[int] = _UNSET,
    ):
        """Initialize the history manager.

        Args:
            recordings_folder: Path to folder for saved recordings.
            max_recordings: Maximum number of recordings to keep, or ``None``
                to keep all. When omitted, loads from settings (default custom
                limit from config).
        """
        self.recordings_folder = recordings_folder or config.RECORDINGS_FOLDER
        if max_recordings is _UNSET:
            self.max_recordings = resolve_max_saved_recordings()
        else:
            self.max_recordings = max_recordings

        # Ensure recordings folder exists
        os.makedirs(self.recordings_folder, exist_ok=True)

        logger.info(
            "HistoryManager initialized (recordings: %s, max: %s)",
            self.recordings_folder,
            self.max_recordings if self.max_recordings is not None else "all",
        )

    def set_max_recordings(self, max_recordings: Optional[int]) -> None:
        """Update the retention limit and rotate immediately if needed.

        Args:
            max_recordings: Maximum recordings to keep, or ``None`` to keep all.
        """
        self.max_recordings = max_recordings
        logger.info(
            "Recording retention updated (max: %s)",
            max_recordings if max_recordings is not None else "all",
        )
        self._rotate_recordings()

    def add_entry(
        self,
        text: str,
        model: str,
        source_audio_path: Optional[str] = None,
        transcription_time: Optional[float] = None,
        audio_duration: Optional[float] = None,
        file_size: Optional[int] = None
    ) -> HistoryEntry:
        """Add a new transcription to history.

        Args:
            text: The transcribed text.
            model: The model used for transcription (display name or internal value).
            source_audio_path: Optional path to source audio file to save.
            transcription_time: Time taken to transcribe in seconds.
            audio_duration: Duration of the audio in seconds.
            file_size: Size of the audio file in bytes.

        Returns:
            The created HistoryEntry.
        """
        saved_audio_path = None

        # Save the audio recording if provided
        if source_audio_path and os.path.exists(source_audio_path):
            saved_audio_path = self._save_recording(source_audio_path)

        # Create the entry
        entry = HistoryEntry.create(
            text=text,
            model=model,
            audio_file=saved_audio_path,
            transcription_time=transcription_time,
            audio_duration=audio_duration,
            file_size=file_size
        )

        # Save to database
        db.add_history_entry(
            entry_id=entry.id,
            text=entry.text,
            timestamp=entry.timestamp,
            model=entry.model,
            audio_file=entry.audio_file,
            transcription_time=entry.transcription_time,
            audio_duration=entry.audio_duration,
            file_size=entry.file_size
        )

        logger.info(f"Added history entry: {entry.id[:8]}...")
        return entry

    def _save_recording(self, source_path: str) -> Optional[str]:
        """Save a recording to the recordings folder with rotation.

        Args:
            source_path: Path to the source audio file.

        Returns:
            Relative path to saved recording, or None if failed.
        """
        try:
            # Generate filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"recording_{timestamp}.wav"
            dest_path = os.path.join(self.recordings_folder, filename)

            # Copy the file
            shutil.copy2(source_path, dest_path)
            logger.info(f"Saved recording: {filename}")

            # Rotate old recordings
            self._rotate_recordings()

            return filename

        except Exception as e:
            logger.error(f"Failed to save recording: {e}")
            return None

    def _rotate_recordings(self) -> None:
        """Remove oldest recordings if we exceed max_recordings."""
        if self.max_recordings is None:
            return

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
                        logger.info(f"Removed old recording: {rec.filename}")

                        # Clear audio_file reference in database
                        db.clear_history_audio_file(rec.filename)

                    except Exception as e:
                        logger.error(f"Failed to remove recording {rec.filename}: {e}")

        except Exception as e:
            logger.error(f"Failed to rotate recordings: {e}")

    def get_history(self, limit: Optional[int] = None) -> List[HistoryEntry]:
        """Get transcription history entries.

        Args:
            limit: Optional maximum number of entries to return.

        Returns:
            List of HistoryEntry objects (newest first).
        """
        return db.get_history_entries(limit)

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
            logger.error(f"Failed to get recordings: {e}")

        return recordings

    def get_entry_by_id(self, entry_id: str) -> Optional[HistoryEntry]:
        """Get a specific history entry by ID.

        Args:
            entry_id: The entry ID to find.

        Returns:
            The HistoryEntry or None if not found.
        """
        return db.get_history_entry_by_id(entry_id)

    def delete_entry(self, entry_id: str) -> bool:
        """Delete a history entry.

        Args:
            entry_id: The entry ID to delete.

        Returns:
            True if deleted, False if not found.
        """
        result = db.delete_history_entry(entry_id)
        if result:
            logger.info(f"Deleted history entry: {entry_id[:8]}...")
        return result

    def clear_history(self) -> None:
        """Clear all history entries (keeps recordings)."""
        db.clear_history()
        logger.info("History cleared")

    def clear_history_and_recordings(self) -> None:
        """Clear all history entries and delete saved recordings from disk."""
        for rec in self.get_recordings():
            try:
                os.remove(rec.file_path)
            except Exception as e:
                logger.error(f"Failed to remove recording {rec.filename}: {e}")
        db.clear_history()
        logger.info("History and recordings cleared")

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


class _LazyHistoryManager:
    """Create the history manager only when history is first used."""

    def __init__(self) -> None:
        self._instance: Optional[HistoryManager] = None

    def _get_instance(self) -> HistoryManager:
        if self._instance is None:
            self._instance = HistoryManager()
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._get_instance(), name)


# Public lazy history manager proxy.
history_manager = _LazyHistoryManager()
