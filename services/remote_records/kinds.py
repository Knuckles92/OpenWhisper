"""Each kind of record as files, and back again.

A record is ``record.json`` plus its audio. ``origin`` names the paired
computer a record belongs to when it is stored on a host, and is None for
this computer's own records, so the same code bundles a record here to
upload it, imports it on the host, and does the reverse when a computer
brings its records back.

Imports treat record.json as untrusted: fields are checked against the
columns they land in and coerced to their types, audio names go through
``safe_name``, and nothing is written outside this computer's own
recordings and meetings folders.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from sqlalchemy import Boolean, Float, Integer, LargeBinary, String, Text, func

from services.remote_records.host_store import MANIFEST, safe_name

logger = logging.getLogger(__name__)

FORMAT = 1
_MAX_TEXT = 2 * 1024 * 1024
_MAX_STRING = 4096


@dataclass
class Bundle:
    """A record ready to send: its record.json and where each file is now."""

    record: dict
    files: Dict[str, str] = field(default_factory=dict)

    def manifest_bytes(self) -> bytes:
        return json.dumps(self.record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _is_wav(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return False
    return len(head) == 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"


def _move(src: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.replace(src, dest)
    except OSError:
        # Staging on another volume than the destination.
        shutil.move(src, dest)


def _coerce(column, value, what: str):
    """``value`` for ``column``, or ValueError when it can't be one."""
    if value is None:
        if not column.nullable and not column.primary_key:
            raise ValueError(f"{what}.{column.name} is required.")
        return None
    kind = type(column.type)
    if issubclass(kind, Boolean):
        if not isinstance(value, bool):
            raise ValueError(f"{what}.{column.name} must be true or false.")
        return value
    if issubclass(kind, Integer):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{what}.{column.name} must be a whole number.")
        return value
    if issubclass(kind, Float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{what}.{column.name} must be a number.")
        return float(value)
    if issubclass(kind, LargeBinary):
        if not isinstance(value, dict) or not isinstance(value.get("$b64"), str):
            raise ValueError(f"{what}.{column.name} must be base64 bytes.")
        return base64.b64decode(value["$b64"], validate=True)
    if issubclass(kind, (Text, String)):
        if not isinstance(value, str):
            raise ValueError(f"{what}.{column.name} must be text.")
        limit = _MAX_TEXT if issubclass(kind, Text) else _MAX_STRING
        if len(value) > limit:
            raise ValueError(f"{what}.{column.name} is too long.")
        return value
    raise ValueError(f"{what}.{column.name} can't be imported.")


def _row_from_json(model, data, what: str, *, drop=(), fixed=None, ignore_unknown=False) -> dict:
    """Column values for ``model`` from untrusted ``data``.

    ``ignore_unknown`` leaves out fields this version has no column for (a
    newer computer's) instead of refusing the record; the fields it knows
    are checked as strictly either way.
    """
    if not isinstance(data, dict):
        raise ValueError(f"{what} must be an object.")
    columns = {column.name: column for column in model.__table__.columns}
    unknown = set(data) - set(columns)
    if unknown and not ignore_unknown:
        raise ValueError(f"{what} has unknown fields: {', '.join(sorted(unknown)[:5])}.")
    if unknown:
        logger.debug("Ignored %d %s field(s) this version doesn't know", len(unknown), what)
    values = {}
    for name, column in columns.items():
        if name in drop:
            continue
        if fixed and name in fixed:
            values[name] = fixed[name]
            continue
        if name not in data:
            if column.default is None and not column.nullable and not column.primary_key:
                raise ValueError(f"{what}.{name} is required.")
            continue
        values[name] = _coerce(column, data[name], what)
    return values


# ---------------------------------------------------------------- dictation

_ENTRY_FIELDS = (
    "id", "text", "raw_text", "timestamp", "model", "transcription_time",
    "audio_duration", "file_size", "cleanup_provider", "cleanup_model", "source_name", "title",
)

#: Columns newer than _ENTRY_FIELDS. They travel in record["entry_ext"],
#: which computers from before them ignore; "entry" itself stays as they
#: expect it, or they would refuse the record. Which app an entry was
#: dictated into stays on this computer.
ENTRY_EXT_FIELDS = ("entry_kind", "cleanup_level", "cleaned_text", "language")


def _entry_ext(entry) -> dict:
    return {name: getattr(entry, name, None) for name in ENTRY_EXT_FIELDS}


def _entry_ext_values(model, data) -> dict:
    """The known, well-formed fields of an untrusted ``entry_ext``.

    Anything else is left out rather than refusing the entry: these fields
    only describe it, and a newer computer may send more of them.
    """
    if not isinstance(data, dict):
        if data is not None:
            logger.debug("Ignored an entry_ext that isn't an object")
        return {}
    columns = {column.name: column for column in model.__table__.columns}
    values = {}
    for name, value in data.items():
        column = columns.get(name) if name in ENTRY_EXT_FIELDS else None
        if column is None:
            logger.debug("Ignored an unknown entry_ext field")
            continue
        try:
            values[name] = _coerce(column, value, "entry_ext")
        except ValueError:
            logger.debug("Ignored a malformed entry_ext field: %s", name)
    return values


class DictationRecords:
    """History entries and their recordings.

    On a host, a device's recordings live in ``recordings/devices/<id>/`` and
    its entries' ``audio_file`` is that relative path, so this computer's own
    retention and "Clear history + recordings", which only look at the top
    of the folder, never touch them.
    """

    kind = "dictation"

    def __init__(self, recordings_folder: Optional[str] = None, database=None):
        self._folder = recordings_folder
        self._db = database

    @property
    def db(self):
        if self._db is None:
            from services.database import db

            self._db = db
        return self._db

    @property
    def root(self) -> str:
        if self._folder is None:
            from config import config

            return os.path.abspath(config.RECORDINGS_FOLDER)
        return os.path.abspath(self._folder)

    def _device_folder(self, device_id: str) -> str:
        return os.path.join(self.root, "devices", device_id)

    def _entry(self, origin, record_id: str):
        entry = self.db.get_history_entry_by_id(record_id)
        if entry is None or entry.origin_device_id != origin:
            raise LookupError("That history entry isn't here anymore.")
        return entry

    def _audio_path(self, entry) -> Optional[str]:
        if not entry.audio_file:
            return None
        path = os.path.abspath(os.path.join(self.root, *entry.audio_file.split("/")))
        inside = self.root if entry.origin_device_id is None else self._device_folder(entry.origin_device_id)
        if os.path.dirname(path) != inside or not os.path.isfile(path):
            return None
        return path

    def owns(self, device_id: str, record_id: str) -> bool:
        entry = self.db.get_history_entry_by_id(record_id)
        return entry is not None and entry.origin_device_id == device_id

    def owners(self) -> list:
        from services.models import TranscriptionHistory as Model

        with self.db.get_session() as session:
            rows = session.query(Model.origin_device_id, func.max(Model.origin_device_name),
                                 func.count(Model.id)).filter(
                Model.origin_device_id.is_not(None)
            ).group_by(Model.origin_device_id).all()
        return [{"id": owner, "name": name or "Unnamed computer", "count": count}
                for owner, name, count in rows]

    # ---- this computer's own ----

    def exists(self, record_id: str) -> bool:
        entry = self.db.get_history_entry_by_id(record_id)
        return entry is not None and entry.origin_device_id is None

    def local_ids(self) -> List[str]:
        return [entry.id for entry in self.db.get_history_entries(origin=None)]

    def ready(self, record_id: str) -> Optional[str]:
        """None when the record can be sent now, else why it has to wait."""
        return None

    def in_use(self, record_id: str) -> bool:
        """Whether something here has the record open, so a move keeps it for now."""
        return False

    def digest(self, record_id: str) -> str:
        """Changes with what the host would store: the text shown, the title, the audio."""
        record = self.bundle(None, record_id).record
        return hashlib.sha256(
            json.dumps(record, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def delete_local(self, record_id: str) -> None:
        from services.history_manager import history_manager

        history_manager.delete_entry(record_id, delete_audio_file=True, kept_on_host=True)

    # ---- both sides ----

    def bundle(self, origin, record_id: str, *, retention: Optional[dict] = None) -> Bundle:
        entry = self._entry(origin, record_id)
        record = {
            "kind": self.kind,
            "format": FORMAT,
            "entry": {name: getattr(entry, name) for name in _ENTRY_FIELDS},
            "entry_ext": _entry_ext(entry),
            "audio": None,
        }
        if retention is not None:
            record["retention"] = retention
        files = {}
        audio = self._audio_path(entry)
        if audio:
            name = f"audio/{os.path.basename(audio)}"
            try:
                safe_name(name)
            except ValueError:
                name = "audio/recording.wav"
            record["audio"] = name
            files[name] = audio
        return Bundle(record, files)

    def export(self, device_id: str, record_id: str, dest_dir: str) -> Dict[str, str]:
        bundle = self.bundle(device_id, record_id)
        with open(os.path.join(dest_dir, MANIFEST), "wb") as handle:
            handle.write(bundle.manifest_bytes())
        return bundle.files

    def import_record(self, record_id: str, files_dir: str, record: dict, device: Optional[dict]) -> dict:
        from services.models import TranscriptionHistory

        origin = device.get("id") if device else None
        values = _row_from_json(
            TranscriptionHistory, record.get("entry"), "entry",
            drop=("audio_file", "origin_device_id", "origin_device_name"),
            ignore_unknown=True,
        )
        values.update(_entry_ext_values(TranscriptionHistory, record.get("entry_ext")))
        if values.get("id") != record_id:
            raise ValueError("The entry's id doesn't match the record.")
        for name in ("text", "timestamp", "model"):
            if not isinstance(values.get(name), str):
                raise ValueError(f"entry.{name} is required.")
        existing = self.db.get_history_entry_by_id(record_id)
        if existing is not None and existing.origin_device_id != origin:
            raise ValueError("A different history entry already has this id.")

        folder = self._device_folder(origin) if origin else self.root
        audio_file = existing.audio_file if existing is not None else None
        audio_name = record.get("audio")
        if audio_name is not None:
            name = safe_name(audio_name)
            parts = name.split("/")
            if len(parts) != 2 or parts[0] != "audio" or not parts[1].lower().endswith(".wav"):
                raise ValueError("The entry's audio must be one WAV file.")
            source = os.path.join(files_dir, *parts)
            if not _is_wav(source):
                raise ValueError("The entry's audio isn't a WAV file.")
            current = self._audio_path(existing) if existing is not None else None
            dest = current or os.path.join(folder, parts[1])
            if current is None:
                stem, ext = os.path.splitext(parts[1])
                suffix = 2
                while os.path.exists(dest):
                    dest = os.path.join(folder, f"{stem}-{suffix}{ext}")
                    suffix += 1
            _move(source, dest)
            rel = os.path.relpath(dest, self.root).replace(os.sep, "/")
            audio_file = rel
        values.update(
            audio_file=audio_file,
            origin_device_id=origin,
            origin_device_name=(str(device.get("name") or "")[:120] if device else None),
        )
        self.db.put_history_entry(TranscriptionHistory(**values))
        if origin:
            self._apply_retention(origin, record.get("retention"))
        return {"audio": bool(audio_file)}

    def _apply_retention(self, device_id: str, retention) -> None:
        """The device's own recording limits, applied to what it keeps here."""
        if not isinstance(retention, dict):
            return
        limits = []
        for key in ("max_recordings", "max_bytes"):
            value = retention.get(key)
            limits.append(value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None)
        max_count, max_bytes = limits
        if max_count is None and max_bytes is None:
            return
        from services.history_manager import HistoryManager

        folder = self._device_folder(device_id)
        manager = HistoryManager(folder, max_recordings=max_count, max_bytes=max_bytes)
        for recording in manager.recordings_over_limit(max_count, max_bytes):
            try:
                os.remove(recording.file_path)
            except OSError:
                logger.warning("Could not remove a paired computer's old recording", exc_info=True)
                continue
            rel = os.path.relpath(recording.file_path, self.root).replace(os.sep, "/")
            self.db.clear_history_audio_file(rel)

    def list(self, device_id: str, query: str, limit: int) -> list:
        if query.strip():
            entries = self.db.search_history_entries(query, limit, origin=device_id)
        else:
            entries = self.db.get_history_entries(limit, origin=device_id)
        result = []
        for entry in entries:
            item = {name: getattr(entry, name) for name in _ENTRY_FIELDS}
            item.update(_entry_ext(entry))
            audio = self._audio_path(entry)
            item["has_audio"] = audio is not None
            item["audio_bytes"] = os.path.getsize(audio) if audio else 0
            result.append(item)
        return result

    def delete(self, device_id: str, record_id: str) -> bool:
        try:
            entry = self._entry(device_id, record_id)
        except LookupError:
            return False
        audio = self._audio_path(entry)
        self.db.delete_history_entry(record_id)
        if audio:
            try:
                os.remove(audio)
            except OSError:
                logger.warning("Could not delete a paired computer's recording", exc_info=True)
        return True

    def delete_all(self, device_id: str) -> int:
        removed = 0
        for entry in self.db.get_history_entries(origin=device_id):
            removed += bool(self.delete(device_id, entry.id))
        shutil.rmtree(self._device_folder(device_id), ignore_errors=True)
        return removed

    def rename_owner(self, device_id: str, name: str) -> int:
        return self.db.rename_history_origin(device_id, name[:120])

    def summary(self, device_id: str) -> dict:
        count = self.db.history_origin_counts().get(device_id, 0)
        folder = self._device_folder(device_id)
        size = 0
        try:
            with os.scandir(folder) as entries:
                size = sum(item.stat().st_size for item in entries if item.is_file())
        except OSError:
            pass
        return {"count": count, "bytes": size}

    def stored_files(self, device_id: str, record_id: str) -> Dict[str, int]:
        """Entries are sent whole; nothing is kept from an earlier copy."""
        return {}


# ---------------------------------------------------------------- meetings

#: Files in a meeting folder that are rebuilt, temporary or crash leftovers.
#: Sending the recovery ones would make the receiving computer "recover" a
#: meeting that ended fine.
_SKIP_SUFFIXES = (".pcm", ".recovery.json", ".orphan", ".tmp")
_SKIP_NAMES = frozenset({"playback.wav", "playback.json"})
_SYNCABLE_STATUSES = frozenset({"ended", "failed"})
_BUSY_STEPS = frozenset({"pending", "running"})
#: Child tables with string ids, copied as they are.
_PLAIN_CHILDREN = ("participants", "state_items", "questions")
_CHILDREN = ("chunks", "segments", *_PLAIN_CHILDREN, "events")
#: Session columns that belong to whichever computer holds the meeting.
_LOCAL_SESSION_COLUMNS = ("host_token", "guest_token", "app_pid", "app_heartbeat_at",
                          "origin_device_id", "origin_device_name", "spool_dir")


def _meeting_models() -> dict:
    from services.models import (
        MeetingAudioChunk,
        MeetingEvent,
        MeetingParticipant,
        MeetingQuestion,
        MeetingSegment,
        MeetingSession,
        MeetingStateItem,
    )

    return {
        "session": MeetingSession, "chunks": MeetingAudioChunk,
        "segments": MeetingSegment, "participants": MeetingParticipant,
        "state_items": MeetingStateItem, "questions": MeetingQuestion,
        "events": MeetingEvent,
    }


def _jsonable(values: dict, skip=()) -> dict:
    return {
        key: ({"$b64": base64.b64encode(bytes(value)).decode("ascii")}
              if isinstance(value, (bytes, bytearray, memoryview)) else value)
        for key, value in values.items() if key not in skip
    }


class MeetingRecords:
    """Meetings: their rows in the meeting tables and their audio folder.

    ``busy(meeting_id)`` says whether something on this computer is still
    writing a meeting (post-meeting steps, a re-run), which keeps it from
    being sent half done; ``in_use(meeting_id)`` whether a dashboard may be
    showing it, which keeps a moved meeting's copy here until it closes.
    Both are set by the app once its meeting runtime exists.
    """

    kind = "meeting"

    def __init__(self, meetings_root: Optional[str] = None, repository=None,
                 busy: Optional[Callable[[str], bool]] = None,
                 in_use: Optional[Callable[[str], bool]] = None):
        self._root = meetings_root
        self._repository = repository
        self.busy = busy or (lambda _meeting_id: False)
        self.in_use_check = in_use or (lambda _meeting_id: False)

    @property
    def repository(self):
        if self._repository is None:
            from meeting.persist.repository import SqlMeetingRepository

            self._repository = SqlMeetingRepository()
        return self._repository

    @property
    def root(self) -> str:
        if self._root is None:
            from config import config

            return os.path.abspath(config.MEETINGS_FOLDER)
        return os.path.abspath(self._root)

    def _meeting(self, origin, record_id: str) -> dict:
        meeting = self.repository.get_meeting(record_id)
        if meeting is None or meeting.get("origin_device_id") != origin:
            raise LookupError("That meeting isn't here anymore.")
        return meeting

    def owns(self, device_id: str, record_id: str) -> bool:
        meeting = self.repository.get_meeting(record_id)
        return meeting is not None and meeting.get("origin_device_id") == device_id

    def owners(self) -> list:
        return self.repository.record_owners()

    def _spool(self, meeting: Optional[dict]) -> Optional[str]:
        """The meeting's folder, if it is where meetings are kept here."""
        spool = str((meeting or {}).get("spool_dir") or "")
        if not spool:
            return None
        spool = os.path.abspath(spool)
        return spool if os.path.dirname(spool) == self.root else None

    # ---- this computer's own ----

    def exists(self, record_id: str) -> bool:
        meeting = self.repository.get_meeting(record_id)
        return meeting is not None and not meeting.get("origin_device_id")

    def local_ids(self) -> List[str]:
        return [str(meeting["id"]) for meeting in self.repository.list_meetings()
                if not meeting.get("origin_device_id")
                and str(meeting.get("status") or "") in _SYNCABLE_STATUSES]

    def ready(self, record_id: str) -> Optional[str]:
        """None once nothing more will be written to the meeting."""
        meeting = self._meeting(None, record_id)
        if str(meeting.get("status") or "") not in _SYNCABLE_STATUSES:
            return "it hasn't finished"
        if self.busy(record_id):
            return "it is still being processed"
        if self.repository.count_unfinished_chunks(record_id):
            return "its audio is still being transcribed"
        try:
            state = json.loads(meeting.get("state_json") or "{}")
        except ValueError:
            state = {}
        state = state if isinstance(state, dict) else {}
        finalization = state.get("finalization")
        if isinstance(finalization, dict) and finalization.get("status") in _BUSY_STEPS:
            return "its insights are still being written"
        review = state.get("insight_review")
        if isinstance(review, dict) and review.get("status") == "running":
            return "its insights are still being reviewed"
        return None

    def in_use(self, record_id: str) -> bool:
        return bool(self.in_use_check(record_id))

    def digest(self, record_id: str) -> str:
        """Changes when anything a viewer sees changes: edits, renames, re-runs."""
        record = self.bundle(None, record_id).record
        return hashlib.sha256(
            json.dumps(record, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def delete_local(self, record_id: str) -> None:
        from meeting.persist.data_lifecycle import delete_meeting_data

        delete_meeting_data(self.repository, record_id, self.root)

    # ---- both sides ----

    def bundle(self, origin, record_id: str) -> Bundle:
        """The meeting's rows and every audio file it needs.

        A sender that learns the other computer already holds some of the
        files (``stored_files``) sets ``update`` and leaves those out.
        """
        meeting = self._meeting(origin, record_id)
        rows = self.repository.export_meeting_rows(record_id)
        if rows is None:
            raise LookupError("That meeting isn't here anymore.")
        spool = self._spool(meeting)
        referenced = {os.path.basename(str(chunk.get("file_path") or "")) for chunk in rows["chunks"]}
        files: Dict[str, str] = {}
        if spool and os.path.isdir(spool):
            for entry in sorted(os.listdir(spool)):
                path = os.path.join(spool, entry)
                if (not os.path.isfile(path) or entry in _SKIP_NAMES
                        or entry.endswith(_SKIP_SUFFIXES)):
                    continue
                if entry not in referenced and not entry.endswith(("_session.wav", "_session.json")):
                    continue
                try:
                    files[safe_name(f"audio/{entry}")] = path
                except ValueError:
                    continue
        chunks = []
        for chunk in rows["chunks"]:
            chunk = _jsonable(chunk)
            chunk["file_path"] = os.path.basename(str(chunk.get("file_path") or ""))
            chunks.append(chunk)
        record = {
            "kind": self.kind,
            "format": FORMAT,
            "update": False,
            "session": _jsonable(rows["session"], skip=_LOCAL_SESSION_COLUMNS),
            "chunks": chunks,
            **{key: [_jsonable(item) for item in rows[key]] for key in _CHILDREN[1:]},
            "files": {name: os.path.getsize(path) for name, path in files.items()},
        }
        return Bundle(record, files)

    def export(self, device_id: str, record_id: str, dest_dir: str) -> Dict[str, str]:
        bundle = self.bundle(device_id, record_id)
        with open(os.path.join(dest_dir, MANIFEST), "wb") as handle:
            handle.write(bundle.manifest_bytes())
        return bundle.files

    def stored_files(self, device_id: str, record_id: str) -> Dict[str, int]:
        """The audio this computer already holds for a device's meeting."""
        try:
            meeting = self._meeting(device_id, record_id)
        except LookupError:
            return {}
        spool = self._spool(meeting)
        if not spool or not os.path.isdir(spool):
            return {}
        stored = {}
        with os.scandir(spool) as entries:
            for item in entries:
                if item.is_file():
                    stored[f"audio/{item.name}"] = item.stat().st_size
        return stored

    def import_record(self, record_id: str, files_dir: str, record: dict,
                      device: Optional[dict]) -> dict:
        from meeting.web.auth import generate_token_pair

        models = _meeting_models()
        origin = device.get("id") if device else None
        session_values = _row_from_json(
            models["session"], record.get("session"), "session", drop=_LOCAL_SESSION_COLUMNS,
        )
        if session_values.get("id") != record_id:
            raise ValueError("The meeting's id doesn't match the record.")
        if session_values.get("status") not in _SYNCABLE_STATUSES:
            raise ValueError("Only finished meetings can be stored.")
        declared = record.get("files")
        if not isinstance(declared, dict):
            raise ValueError("The meeting's file list is missing.")
        sizes: Dict[str, int] = {}
        for name, size in declared.items():
            parts = safe_name(name).split("/")
            if len(parts) != 2 or parts[0] != "audio":
                raise ValueError("A meeting's files must be its audio folder.")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError("A meeting file's size is invalid.")
            sizes[parts[1]] = size
        update = record.get("update") is True

        existing = self.repository.get_meeting(record_id)
        if existing is not None and existing.get("origin_device_id") != origin:
            raise ValueError("A different meeting already has this id.")
        if update and existing is None:
            raise LookupError("Send the whole meeting; this computer doesn't have it yet.")
        staged = os.path.join(files_dir, "audio")
        old_spool = self._spool(existing)
        for name, size in sizes.items():
            source = os.path.join(staged, name)
            if os.path.isfile(source):
                if name.lower().endswith(".wav") and not _is_wav(source):
                    raise ValueError(f"The meeting's {name} isn't a WAV file.")
                continue
            kept = os.path.join(old_spool, name) if (update and old_spool) else ""
            if not kept or not os.path.isfile(kept) or os.path.getsize(kept) != size:
                raise ValueError(f"The meeting's {name} is missing.")

        os.makedirs(self.root, exist_ok=True)
        if update and old_spool:
            spool = old_spool
        else:
            spool = os.path.join(self.root, record_id[:12])
            if os.path.exists(spool) and spool != old_spool:
                spool = os.path.join(self.root, f"{record_id[:12]}-{uuid.uuid4().hex[:6]}")
        host_token, guest_token = generate_token_pair()
        session_values.update(
            host_token=host_token, guest_token=guest_token,
            app_pid=None, app_heartbeat_at=None, spool_dir=spool,
            origin_device_id=origin,
            origin_device_name=(str(device.get("name") or "")[:120] if device else None),
        )
        rows = self._children(record, record_id, spool, models)

        tombstone = None
        placed = False
        try:
            if update and old_spool:
                for name in sizes:
                    source = os.path.join(staged, name)
                    if os.path.isfile(source):
                        _move(source, os.path.join(spool, name))
            else:
                if old_spool and os.path.isdir(old_spool):
                    tombstone = os.path.join(self.root, f".deleting-{record_id}-{uuid.uuid4().hex}")
                    os.replace(old_spool, tombstone)
                    os.utime(tombstone, None)
                if os.path.isdir(staged):
                    _move(staged, spool)
                else:
                    os.makedirs(spool, exist_ok=True)
                placed = True
            self.repository.replace_meeting_rows(record_id, session_values, rows)
        except BaseException:
            if placed:
                shutil.rmtree(spool, ignore_errors=True)
            if tombstone and not os.path.exists(old_spool):
                try:
                    os.replace(tombstone, old_spool)
                    tombstone = None
                except OSError:
                    logger.exception("Could not restore a meeting's audio after a failed import")
            raise
        if tombstone:
            shutil.rmtree(tombstone, ignore_errors=True)
        return {"files": len(sizes)}

    def _children(self, record: dict, record_id: str, spool: str, models: dict) -> dict:
        rows: Dict[str, list] = {}
        fixed = {"meeting_id": record_id}
        refs = set()
        for key in _CHILDREN:
            items = record.get(key) or []
            if not isinstance(items, list):
                raise ValueError(f"The meeting's {key} must be a list.")
            validated = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(f"The meeting's {key} must be objects.")
                if key in ("chunks", "events"):
                    item = {k: v for k, v in item.items() if k != "id"}
                values = _row_from_json(models[key], item, key, fixed=fixed,
                                        drop=("id",) if key in ("chunks", "events") else ())
                if key == "chunks":
                    ref = record[key][len(validated)].get("id")
                    if isinstance(ref, bool) or not isinstance(ref, int):
                        raise ValueError("A meeting chunk has no id.")
                    name = values.get("file_path") or ""
                    if not name or os.path.basename(name) != name:
                        raise ValueError("A meeting chunk's file isn't in its folder.")
                    values["file_path"] = os.path.join(spool, name)
                    values["chunk_ref"] = ref
                    refs.add(ref)
                validated.append(values)
            rows[key] = validated
        for segment in rows["segments"]:
            if segment.get("chunk_id") not in refs:
                segment["chunk_id"] = None
        return rows

    def list(self, device_id: str, query: str, limit: int) -> list:
        meetings = self.repository.list_past_meeting_summaries(
            limit=limit, query=query, origin=device_id,
        )
        return [_meeting_summary(meeting) for meeting in meetings]

    def delete(self, device_id: str, record_id: str) -> bool:
        from meeting.persist.data_lifecycle import delete_meeting_data

        try:
            self._meeting(device_id, record_id)
        except LookupError:
            return False
        return bool(delete_meeting_data(self.repository, record_id, self.root))

    def delete_all(self, device_id: str) -> int:
        removed = 0
        for meeting_id, _spool in self.repository.origin_spools(device_id):
            removed += bool(self.delete(device_id, meeting_id))
        return removed

    def rename_owner(self, device_id: str, name: str) -> int:
        return self.repository.rename_origin(device_id, name[:120])

    def summary(self, device_id: str) -> dict:
        spools = self.repository.origin_spools(device_id)
        size = 0
        for _meeting_id, spool in spools:
            spool = self._spool({"spool_dir": spool})
            if spool and os.path.isdir(spool):
                with os.scandir(spool) as entries:
                    size += sum(item.stat().st_size for item in entries if item.is_file())
        return {"count": len(spools), "bytes": size}


def _meeting_summary(meeting: dict) -> dict:
    """What a Past Meetings card needs, without the meeting's access tokens."""
    try:
        state = json.loads(meeting.get("state_json") or "{}")
    except ValueError:
        state = {}
    state = state if isinstance(state, dict) else {}
    topic = state.get("topic") if isinstance(state.get("topic"), dict) else {}
    light_state = {
        "title": state.get("title") or "",
        "topic": {"current": topic.get("current") or ""},
        "finalization": state.get("finalization"),
    }
    item = {key: meeting.get(key) for key in (
        "id", "title", "status", "started_at", "ended_at", "paused_total_s",
        "cloud_enabled", "asr_model",
    )}
    item["state_json"] = json.dumps(light_state, ensure_ascii=False)
    item["content_summary"] = meeting.get("content_summary") or {}
    return item


