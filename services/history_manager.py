"""Transcription history and retained recording management."""
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
        return format_timestamp(self.timestamp)

    @property
    def formatted_size(self) -> str:
        return format_file_size(self.size_bytes)


class HistoryManager:
    """Manages transcription history and saved recordings."""

    def __init__(
        self,
        recordings_folder: str = None,
        max_recordings: Optional[int] = _UNSET,
    ):
        """Use saved retention when ``max_recordings`` is omitted; None keeps all."""
        self.recordings_folder = recordings_folder or config.RECORDINGS_FOLDER
        if max_recordings is _UNSET:
            self.max_recordings = resolve_max_saved_recordings()
        else:
            self.max_recordings = max_recordings

        os.makedirs(self.recordings_folder, exist_ok=True)

        logger.info(
            "HistoryManager initialized (recordings: %s, max: %s)",
            self.recordings_folder,
            self.max_recordings if self.max_recordings is not None else "all",
        )

    def set_max_recordings(self, max_recordings: Optional[int]) -> None:
        """Apply a retention limit immediately; None keeps all recordings."""
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
        file_size: Optional[int] = None,
        raw_text: Optional[str] = None,
        cleanup_provider: Optional[str] = None,
        cleanup_model: Optional[str] = None,
        source_name: Optional[str] = None,
    ) -> HistoryEntry:
        """Persist a transcription and optionally retain its source audio."""
        saved_audio_path = None

        if source_audio_path and os.path.exists(source_audio_path):
            saved_audio_path = self._save_recording(source_audio_path)

        entry = HistoryEntry.create(
            text=text,
            model=model,
            audio_file=saved_audio_path,
            transcription_time=transcription_time,
            audio_duration=audio_duration,
            file_size=file_size,
            raw_text=raw_text,
            cleanup_provider=cleanup_provider,
            cleanup_model=cleanup_model,
            source_name=source_name,
        )

        db.add_history_entry(
            entry_id=entry.id,
            text=entry.text,
            timestamp=entry.timestamp,
            model=entry.model,
            audio_file=entry.audio_file,
            transcription_time=entry.transcription_time,
            audio_duration=entry.audio_duration,
            file_size=entry.file_size,
            raw_text=entry.raw_text,
            cleanup_provider=entry.cleanup_provider,
            cleanup_model=entry.cleanup_model,
            source_name=entry.source_name,
        )

        logger.info(f"Added history entry: {entry.id[:8]}...")
        return entry

    def _save_recording(self, source_path: str) -> Optional[str]:
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"recording_{timestamp}.wav"
            dest_path = os.path.join(self.recordings_folder, filename)

            shutil.copy2(source_path, dest_path)
            logger.info(f"Saved recording: {filename}")

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
                recordings.sort(key=lambda r: r.timestamp)

                to_remove = recordings[:-self.max_recordings]
                for rec in to_remove:
                    try:
                        os.remove(rec.file_path)
                        logger.info(f"Removed old recording: {rec.filename}")

                        db.clear_history_audio_file(rec.filename)

                    except Exception as e:
                        logger.error(f"Failed to remove recording {rec.filename}: {e}")

        except Exception as e:
            logger.error(f"Failed to rotate recordings: {e}")

    def get_history(self, limit: Optional[int] = None) -> List[HistoryEntry]:
        """Return history entries newest first."""
        return db.get_history_entries(limit)

    def get_recordings(self) -> List[RecordingInfo]:
        """Return saved recordings newest first."""
        recordings = []

        try:
            if not os.path.exists(self.recordings_folder):
                return recordings

            for filename in os.listdir(self.recordings_folder):
                if filename.endswith('.wav'):
                    file_path = os.path.join(self.recordings_folder, filename)

                    stat = os.stat(file_path)

                    try:
                        parts = filename.replace('recording_', '').replace('.wav', '')
                        dt = datetime.strptime(parts, "%Y%m%d_%H%M%S")
                        timestamp = dt.isoformat()
                    except Exception:
                        timestamp = datetime.fromtimestamp(stat.st_mtime).isoformat()

                    recordings.append(RecordingInfo(
                        filename=filename,
                        timestamp=timestamp,
                        file_path=file_path,
                        size_bytes=stat.st_size
                    ))

            recordings.sort(key=lambda r: r.timestamp, reverse=True)

        except Exception as e:
            logger.error(f"Failed to get recordings: {e}")

        return recordings

    def get_entry_by_id(self, entry_id: str) -> Optional[HistoryEntry]:
        """Return a history entry by ID, or None."""
        return db.get_history_entry_by_id(entry_id)

    def delete_entry(
        self,
        entry_id: str,
        delete_audio_file: bool = False,
    ) -> bool:
        """Delete an entry and optionally its retained audio."""
        entry = db.get_history_entry_by_id(entry_id) if delete_audio_file else None
        result = db.delete_history_entry(entry_id)
        if result:
            logger.info(f"Deleted history entry: {entry_id[:8]}...")
            if entry and entry.audio_file:
                self._delete_recording_file(entry.audio_file)
        return result

    def _delete_recording_file(self, filename: str) -> bool:
        """Delete a saved recording and clear any remaining database references."""
        audio_path = self.get_recording_path(filename)
        if not audio_path:
            db.clear_history_audio_file(filename)
            logger.info("Saved recording already absent: %s", filename)
            return True

        try:
            os.remove(audio_path)
        except OSError as exc:
            logger.error("Failed to delete saved recording %s: %s", filename, exc)
            return False

        db.clear_history_audio_file(filename)
        logger.info("Deleted saved recording: %s", filename)
        return True

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
        """Return the recording path if it exists."""
        if not filename:
            return None

        file_path = os.path.join(self.recordings_folder, filename)
        if os.path.exists(file_path):
            return file_path
        return None


class _LazyHistoryManager:
    def __init__(self) -> None:
        self._instance: Optional[HistoryManager] = None

    def _get_instance(self) -> HistoryManager:
        if self._instance is None:
            self._instance = HistoryManager()
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._get_instance(), name)

history_manager = _LazyHistoryManager()
