"""Transcription history and retained recording management."""
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from typing import List, Optional, Tuple
from dataclasses import dataclass

from config import config
from services.database import db
from services.format_utils import format_file_size, format_timestamp
from services.models import HISTORY_CONTEXT_COLUMNS
from services.models import TranscriptionHistory as HistoryEntry
from services.settings import (
    resolve_max_saved_recordings,
    resolve_max_saved_recordings_bytes,
    settings_manager,
)

logger = logging.getLogger(__name__)

# Sentinel so callers can pass ``max_recordings=None`` for keep-all.
_UNSET = object()


def _describe_retention(
    max_recordings: Optional[int], max_bytes: Optional[int]
) -> str:
    limits = []
    if max_recordings is not None:
        limits.append(str(max_recordings))
    if max_bytes is not None:
        limits.append(format_file_size(max_bytes))
    return ", ".join(limits) or "all"


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


class HistoryManager:
    """Manages transcription history and saved recordings."""

    def __init__(
        self,
        recordings_folder: str = None,
        max_recordings: Optional[int] = _UNSET,
        max_bytes: Optional[int] = _UNSET,
    ):
        """Use saved retention when no limit is passed; None disables a limit.

        Passing either limit describes the whole policy, so an omitted one is
        off rather than read from settings.
        """
        self.recordings_folder = recordings_folder or config.RECORDINGS_FOLDER
        self._usage_lock = threading.Lock()
        self._usage_cache = None
        self._usage_signature = None
        self._usage_checked_at = 0.0
        if max_recordings is _UNSET and max_bytes is _UNSET:
            settings = settings_manager.load_all_settings()
            self.max_recordings = resolve_max_saved_recordings(settings)
            self.max_bytes = resolve_max_saved_recordings_bytes(settings)
        else:
            self.max_recordings = (
                None if max_recordings is _UNSET else max_recordings
            )
            self.max_bytes = None if max_bytes is _UNSET else max_bytes

        os.makedirs(self.recordings_folder, exist_ok=True)

        logger.info(
            "HistoryManager initialized (recordings: %s, max: %s)",
            self.recordings_folder,
            _describe_retention(self.max_recordings, self.max_bytes),
        )

    def set_retention(
        self,
        max_recordings: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> None:
        """Apply retention limits immediately; None disables that limit."""
        self.max_recordings = max_recordings
        self.max_bytes = max_bytes
        logger.info(
            "Recording retention updated (max: %s)",
            _describe_retention(max_recordings, max_bytes),
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
        **columns,
    ) -> HistoryEntry:
        """Persist a transcription and optionally retain its source audio.

        ``columns`` may carry any of HISTORY_CONTEXT_COLUMNS; other keys are
        dropped, so callers can pass a job's fields without vetting them.
        """
        context = {
            key: value for key, value in columns.items()
            if key in HISTORY_CONTEXT_COLUMNS and value is not None
        }
        ignored = sorted(set(columns) - set(HISTORY_CONTEXT_COLUMNS))
        if ignored:
            logger.debug("Ignored unknown history fields: %s", ", ".join(ignored))
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
            **context,
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
            **context,
        )

        logger.info(f"Added history entry: {entry.id[:8]}...")
        _record_sync().record_saved("dictation", entry.id)
        return entry

    def _save_recording(self, source_path: str) -> Optional[str]:
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = f"recording_{timestamp}"
            suffix = 1
            while True:
                filename = f"{stem}.wav" if suffix == 1 else f"{stem}-{suffix}.wav"
                dest_path = os.path.join(self.recordings_folder, filename)
                try:
                    dest_fd = os.open(
                        dest_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError:
                    suffix += 1
                    continue
                break

            try:
                with os.fdopen(dest_fd, "wb") as destination, open(
                    source_path, "rb"
                ) as source:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
            except Exception:
                try:
                    os.remove(dest_path)
                except FileNotFoundError:
                    pass
                raise
            self._invalidate_recordings_usage()
            logger.info(f"Saved recording: {filename}")

            self._rotate_recordings()

            return filename

        except Exception as e:
            logger.error(f"Failed to save recording: {e}")
            return None

    def preserve_recording(self, source_path: str) -> Optional[str]:
        """Retain standalone audio, such as a failed quick transcription."""
        if not source_path or not os.path.isfile(source_path):
            return None
        return self._save_recording(source_path)

    def _rotate_recordings(self) -> None:
        """Remove the oldest recordings beyond the count or folder-size limit."""
        if self.max_recordings is None and self.max_bytes is None:
            return

        try:
            expired = self.recordings_over_limit(self.max_recordings, self.max_bytes)
            for rec in expired:
                try:
                    os.remove(rec.file_path)
                    self._invalidate_recordings_usage()
                    logger.info(f"Removed old recording: {rec.filename}")

                    db.clear_history_audio_file(rec.filename)

                except Exception as e:
                    logger.error(f"Failed to remove recording {rec.filename}: {e}")

        except Exception as e:
            logger.error(f"Failed to rotate recordings: {e}")

    def recordings_over_limit(
        self,
        max_recordings: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> List[RecordingInfo]:
        """Return the recordings these limits would delete, newest first.

        Rotation deletes exactly this list, so callers can preview a change
        before applying it. The newest recording is always kept, even when it
        alone exceeds the size cap, so the entry just saved keeps its audio.
        """
        recordings = self.get_recordings()
        keep = len(recordings)
        if max_recordings is not None:
            keep = min(keep, max_recordings)
        if max_bytes is not None:
            total = 0
            for index, rec in enumerate(recordings[:keep]):
                total += rec.size_bytes
                if index > 0 and total > max_bytes:
                    keep = index
                    break
        return recordings[keep:]

    def get_recordings_usage(self) -> Tuple[int, int]:
        """Return ``(count, total_bytes)`` for saved recordings."""
        with self._usage_lock:
            try:
                stat = os.stat(self.recordings_folder)
                signature = (self.recordings_folder, stat.st_mtime_ns, stat.st_ctime_ns)
            except OSError:
                signature = None
            now = time.monotonic()
            if (self._usage_cache is not None and signature == self._usage_signature
                    and now - self._usage_checked_at < 2.0):
                return self._usage_cache
            count = total_bytes = 0
            try:
                with os.scandir(self.recordings_folder) as entries:
                    for entry in entries:
                        if not entry.name.endswith('.wav'):
                            continue
                        try:
                            if entry.is_file():
                                count += 1
                                total_bytes += entry.stat().st_size
                        except FileNotFoundError:
                            continue  # A concurrent retention pass removed it.
            except FileNotFoundError:
                pass
            self._usage_cache = (count, total_bytes)
            self._usage_signature = signature
            self._usage_checked_at = now
            return self._usage_cache

    def _invalidate_recordings_usage(self) -> None:
        with self._usage_lock:
            self._usage_cache = None

    def get_history(self, limit: Optional[int] = None) -> List[HistoryEntry]:
        """Return history entries newest first."""
        return db.get_history_entries(limit)

    def search_history(
        self,
        query: str,
        limit: Optional[int] = None,
    ) -> List[HistoryEntry]:
        """Return matching history entries newest first, optionally paged."""
        return db.search_history_entries(query, limit)

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
            _record_sync().record_deleted("dictation", entry_id)
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
            self._invalidate_recordings_usage()
        except OSError as exc:
            logger.error("Failed to delete saved recording %s: %s", filename, exc)
            return False

        db.clear_history_audio_file(filename)
        logger.info("Deleted saved recording: %s", filename)
        return True

    def clear_history(self) -> None:
        """Clear all history entries (keeps recordings), here and on the host."""
        db.clear_history()
        _record_sync().cleared("dictation")
        logger.info("History cleared")

    def clear_history_and_recordings(self) -> None:
        """Clear all history entries and delete saved recordings from disk."""
        for rec in self.get_recordings():
            try:
                os.remove(rec.file_path)
            except Exception as e:
                logger.error(f"Failed to remove recording {rec.filename}: {e}")
        db.clear_history()
        _record_sync().cleared("dictation")
        logger.info("History and recordings cleared")

    def get_recording_path(self, filename: str) -> Optional[str]:
        """Return the recording path if it exists."""
        if not filename:
            return None

        file_path = os.path.join(self.recordings_folder, filename)
        if os.path.exists(file_path):
            return file_path
        return None


def _record_sync():
    """The outbox that copies records to a paired host (services/remote_records)."""
    from services.remote_records.sync import record_sync

    return record_sync


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
