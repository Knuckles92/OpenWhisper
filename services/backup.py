"""Portable, offline backups of OpenWhisper's *owned* user data.

The bundle is a ZIP with a checksummed manifest.  It deliberately contains no
OS credential-store entries, ``.env``, TLS private key, downloaded models,
logs, or cache.  Pairings and network service opt-ins are reset in the copied
settings and database; a restored computer must be paired again.  Records
kept only on a paired host are not available to a local backup: bring them
back before exporting if they must be included.

Restore is staged while the app is running and applied at the next startup,
before settings or SQLite are opened.  Only names in ``OWNED_NAMES`` may be
moved.  In particular, a source checkout may itself be ``data_root`` and is
never replaced as a directory.  Previous owned data is kept under
``.openwhisper-restore/previous-*`` after a successful restore.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

FORMAT = "openwhisper-backup"
FORMAT_VERSION = 1
ARCHIVE_EXTENSION = ".owbackup"
MANIFEST_NAME = "manifest.json"
RESTORE_DIR = ".openwhisper-restore"
PENDING_NAME = "pending.json"
JOURNAL_NAME = "journal.json"
LAST_NAME = "last_restore.json"
PATH_MARKER = "__OW_BACKUP_DATA__/"
OWNED_NAMES = (
    "openwhisper_settings.json",
    "openwhisper.db",
    "transcription_history.json",
    "transcription_history.json.bak",
    "meetings.json",
    "meetings.json.bak",
    "recorded_audio.wav",
    ".recorded_audio-recovery",
    "recordings",
    "meetings",
    "remote_records",
    "scratchpad.txt",
)
SQLITE_SIDECARS = ("openwhisper.db-wal", "openwhisper.db-shm",
                   "openwhisper.db-journal")
RESTORE_NAMES = OWNED_NAMES + SQLITE_SIDECARS
RECORDING_NAMES = frozenset({
    "recorded_audio.wav", ".recorded_audio-recovery", "recordings", "meetings",
    "remote_records",
})
LEGACY_HISTORY_NAMES = frozenset({"transcription_history.json",
                                  "transcription_history.json.bak"})
MAX_FILES = 100_000
MAX_TOTAL_BYTES = 1 << 40  # 1 TiB; actual extraction is also disk-space checked.
MAX_FILE_BYTES = 1 << 36
MAX_MANIFEST_BYTES = 8 << 20
MAX_RATIO = 10_000
CHUNK_SIZE = 1 << 20
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BackupError(RuntimeError):
    """The bundle is invalid or an operation could not complete safely."""


class BackupCancelled(BackupError):
    """A caller canceled before the bundle or restore was published."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: str
    app_version: str
    schema_version: int
    include_recordings: bool
    file_count: int
    total_bytes: int
    archive_bytes: int
    warnings: tuple[str, ...] = ()


def _root(data_dir: str | os.PathLike[str] | None) -> Path:
    if data_dir is None:
        # Intentionally lazy: apply_pending_restore is called before normal
        # application imports when its caller supplies the resolved root.
        from config import data_root

        data_dir = data_root()
    root = Path(data_dir).expanduser().absolute()
    if _is_link(root):
        raise BackupError("The data directory is a symbolic link or junction")
    return root


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _check_cancel(cancel: Any) -> None:
    if cancel is None:
        return
    stopped = cancel() if callable(cancel) else cancel.is_set()
    if stopped:
        raise BackupCancelled("Backup canceled")


def _progress(callback: Callable[[str, int, int], None] | None,
              phase: str, done: int, total: int) -> None:
    if callback is not None:
        callback(phase, done, total)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.absolute().relative_to(parent.absolute())
        return True
    except ValueError:
        return False


def _safe_member(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise BackupError("Unsafe archive member name")
    if name.startswith("/") or any(ord(c) < 32 for c in name):
        raise BackupError("Unsafe archive member name")
    parts = name.split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise BackupError("Unsafe archive member name")
    if str(PurePosixPath(name)) != name:
        raise BackupError("Noncanonical archive member name")
    if name == MANIFEST_NAME:
        return name
    if len(parts) < 2 or parts[0] != "data" or parts[1] not in OWNED_NAMES:
        raise BackupError("Archive contains a file outside owned data")
    if parts[1] in LEGACY_HISTORY_NAMES or parts[1] == "remote_records":
        raise BackupError("Archive contains transient or raw legacy data")
    if len(parts) > 2 and parts[1] not in RECORDING_NAMES:
        raise BackupError("Archive contains an unexpected nested file")
    if parts[1] == "remote_records" and len(parts) > 2 and parts[2] == "cache":
        raise BackupError("Archive contains remote cache")
    if parts[1] == RESTORE_DIR:
        raise BackupError("Archive contains internal restore data")
    return name


def _safe_path(root: Path, name: str) -> Path:
    # SQLite sidecars are moved during restore but never exported; the online
    # backup API has already folded committed WAL pages into the snapshot.
    if (name not in SQLITE_SIDECARS and name not in LEGACY_HISTORY_NAMES
            and name != "remote_records"):
        _safe_member("data/" + name)
    path = root.joinpath(*name.split("/"))
    if not _inside(path, root):
        raise BackupError("Path escapes the data directory")
    cursor = root
    for part in name.split("/"):
        cursor = cursor / part
        if _is_link(cursor):
            raise BackupError(f"Symbolic links are not supported in backed-up data: {name}")
    return path


def _owned_files(root: Path, include_recordings: bool) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for name in OWNED_NAMES:
        if name in ("openwhisper.db", "openwhisper_settings.json"):
            continue  # snapshot and sanitized copy are added separately
        if name in LEGACY_HISTORY_NAMES:
            continue  # live legacy entries are merged into the DB snapshot
        if name == "remote_records":
            continue  # staging, exports, and cache are transient sync data
        if not include_recordings and name in RECORDING_NAMES:
            continue
        base = _safe_path(root, name)
        if _is_link(base):
            raise BackupError(f"Symbolic links are not supported in backed-up data: {name}")
        if not base.exists():
            continue
        if base.is_file():
            if not stat.S_ISREG(base.stat().st_mode):
                raise BackupError(f"Unsupported data file: {name}")
            files.append((f"data/{name}", base))
            continue
        if not base.is_dir():
            raise BackupError(f"Unsupported data path: {name}")
        for folder, dirs, names in os.walk(base, followlinks=False):
            folder_path = Path(folder)
            dirs[:] = sorted(d for d in dirs if not (name == "remote_records" and
                              folder_path == base and d == "cache"))
            for directory in dirs:
                if _is_link(folder_path / directory):
                    raise BackupError("Symbolic links are not supported in backed-up data")
            for filename in sorted(names):
                path = folder_path / filename
                if _is_link(path) or not stat.S_ISREG(path.stat().st_mode):
                    raise BackupError("Symbolic links and special files cannot be backed up")
                rel = path.relative_to(root).as_posix()
                _safe_member("data/" + rel)
                files.append(("data/" + rel, path))
    return files


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if _is_link(path) or not path.is_file():
        raise BackupError("Settings path is unsafe")
    try:
        with path.open("r", encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, ValueError) as exc:
        raise BackupError("Settings JSON cannot be read") from exc
    if not isinstance(settings, dict):
        raise BackupError("Settings JSON must be an object")
    return settings


#: Settings whose text is the user's own content, where a URL is something
#: they typed to paste later rather than an endpoint that may hold a secret.
_URL_SCRUB_EXEMPT = frozenset({"dictation_snippets"})


def _sanitize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    # Keyring values are not in this JSON. Clear metadata that would point to
    # missing tokens/certificates, and disable services on the restored host.
    cleaned = dict(settings)
    for key in ("remote_engine_client", "remote_host_devices",
                "meeting_context_folder_path"):
        cleaned.pop(key, None)
    cleaned.update({
        "remote_host_enabled": False,
        "remote_host_model_management": False,
        "remote_host_tailscale_trust": False,
        "remote_host_keep_records": False,
        "remote_host_manage_mcp": False,
        "remote_client_history": False,
        "remote_records_location": "local",
        "mcp_enabled": False,
        "mcp_tailscale_enabled": False,
        "meeting_server_bind": "localhost",
        "meeting_context_folder_enabled": False,
        "meeting_context_folder_path": "",
    })
    # Defense in depth for older/extension settings that may store a secret.
    def scrub(value: Any, keep_urls: bool = False) -> Any:
        if isinstance(value, dict):
            return {key: scrub(item, keep_urls or key in _URL_SCRUB_EXEMPT)
                    for key, item in value.items()
                    if isinstance(key, str) and not any(
                        word in key.lower() for word in
                        ("password", "secret", "private_key", "api_key", "credential",
                         "token", "authorization", "header", "cookie")
                    )}
        if isinstance(value, list):
            return [scrub(item, keep_urls) for item in value]
        if isinstance(value, str) and not keep_urls:
            try:
                parts = urlsplit(value)
            except ValueError:
                return "" if value.lower().startswith(("http://", "https://")) else value
            if parts.scheme in ("http", "https") and parts.netloc:
                # Endpoint URLs can carry credentials in userinfo or query.
                return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1],
                                   parts.path, "", ""))
        return value
    return scrub(cleaned)


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}


def _db_schema(conn: sqlite3.Connection) -> int:
    if not _table(conn, "schema_version"):
        return 0
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    return int(row[0]) if row is not None else 0


def _current_schema() -> int:
    # Lazy: this imports SQLAlchemy, but only for create/inspect/prepare, never
    # for early startup apply_pending_restore.
    from services.database import SCHEMA_VERSION

    return SCHEMA_VERSION


def _quick_check(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise BackupError("Backup database failed SQLite integrity check")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise BackupError("Backup database has broken foreign keys")


def _meeting_is_live(conn: sqlite3.Connection) -> bool:
    if not _table(conn, "meeting_sessions") or "status" not in _columns(conn, "meeting_sessions"):
        return False
    return conn.execute("SELECT 1 FROM meeting_sessions WHERE status IN ('active','paused') LIMIT 1").fetchone() is not None


def _snapshot_database(source: Path, output: Path, cancel: Any) -> int:
    if source.exists():
        if _is_link(source) or not source.is_file():
            raise BackupError("Database path is unsafe")
        read = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            if _meeting_is_live(read):
                raise BackupError("End the live meeting before creating a backup")
            with closing(sqlite3.connect(output)) as write:
                read.backup(write, pages=256,
                            progress=lambda *_: _check_cancel(cancel), sleep=0.05)
        finally:
            read.close()
    else:
        # A legacy-only data root still needs a complete current schema so
        # imported history survives the next DatabaseManager initialization.
        from sqlalchemy import create_engine

        from services.models import Base

        engine = create_engine(f"sqlite:///{output}")
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        with closing(sqlite3.connect(output)) as conn, conn:
            conn.execute("INSERT INTO schema_version VALUES (?)", (_current_schema(),))
    with closing(sqlite3.connect(output)) as conn:
        _quick_check(conn)
        return _db_schema(conn)


def _relative_under(path_text: str, root: Path, group: str) -> str | None:
    path = Path(path_text)
    if not path.is_absolute():
        path = root / path
    try:
        rel = path.absolute().relative_to(root.absolute())
    except ValueError:
        return None
    if not rel.parts or rel.parts[0] != group:
        return None
    value = rel.as_posix()
    _safe_member("data/" + value)
    return value


def _sanitize_database(path: Path, source_root: Path,
                       include_recordings: bool) -> tuple[int, list[str], set[str]]:
    warnings: list[str] = []
    required_assets: set[str] = set()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        version = _db_schema(conn)
        if version > _current_schema():
            raise BackupError("Database schema is newer than this app")
        if _table(conn, "meeting_sessions"):
            cols = _columns(conn, "meeting_sessions")
            if {"id", "spool_dir"} <= cols:
                rows = conn.execute("SELECT id, spool_dir FROM meeting_sessions").fetchall()
                for meeting_id, spool in rows:
                    rel = _relative_under(str(spool or ""), source_root, "meetings")
                    if rel is None:
                        if include_recordings:
                            raise BackupError("A meeting stores audio outside the data directory")
                        rel = f"meetings/{meeting_id}"
                    updates: dict[str, Any] = {"spool_dir": PATH_MARKER + rel}
                    if "host_token" in cols:
                        updates["host_token"] = secrets.token_urlsafe(32)
                    if "guest_token" in cols:
                        updates["guest_token"] = secrets.token_urlsafe(32)
                    if "asr_remote_json" in cols:
                        updates["asr_remote_json"] = None
                    if "agent_endpoint_json" in cols:
                        updates["agent_endpoint_json"] = None
                    if "app_pid" in cols:
                        updates["app_pid"] = None
                    if "app_heartbeat_at" in cols:
                        updates["app_heartbeat_at"] = None
                    sql = "UPDATE meeting_sessions SET " + ", ".join(f"{key}=?" for key in updates) + " WHERE id=?"
                    conn.execute(sql, (*updates.values(), meeting_id))
        if _table(conn, "meeting_audio_chunks"):
            if include_recordings:
                for chunk_id, file_path in conn.execute("SELECT id, file_path FROM meeting_audio_chunks").fetchall():
                    rel = _relative_under(str(file_path or ""), source_root, "meetings")
                    if rel is None:
                        raise BackupError("A meeting chunk points outside the data directory")
                    if not (source_root / Path(rel)).is_file():
                        raise BackupError(f"Meeting chunk {chunk_id} has no local audio file")
                    required_assets.add("data/" + rel)
                    conn.execute("UPDATE meeting_audio_chunks SET file_path=? WHERE id=?",
                                 (PATH_MARKER + rel, chunk_id))
            else:
                if _table(conn, "meeting_segments") and "chunk_id" in _columns(conn, "meeting_segments"):
                    conn.execute("UPDATE meeting_segments SET chunk_id=NULL")
                conn.execute("DELETE FROM meeting_audio_chunks")
        if _table(conn, "transcription_history") and "audio_file" in _columns(conn, "transcription_history"):
            if not include_recordings:
                conn.execute("UPDATE transcription_history SET audio_file=NULL")
            else:
                for row_id, audio in conn.execute("SELECT id, audio_file FROM transcription_history WHERE audio_file IS NOT NULL").fetchall():
                    if not isinstance(audio, str):
                        conn.execute("UPDATE transcription_history SET audio_file=NULL WHERE id=?", (row_id,))
                        continue
                    if Path(audio).is_absolute():
                        rel = _relative_under(audio, source_root, "recordings")
                        if rel is None:
                            warnings.append(f"History entry {row_id} refers to external audio")
                            conn.execute("UPDATE transcription_history SET audio_file=NULL WHERE id=?", (row_id,))
                        else:
                            if (source_root / Path(rel)).is_file():
                                required_assets.add("data/" + rel)
                                conn.execute("UPDATE transcription_history SET audio_file=? WHERE id=?",
                                             (rel.removeprefix("recordings/"), row_id))
                            else:
                                warnings.append(f"History entry {row_id} has no local audio file")
                                conn.execute("UPDATE transcription_history SET audio_file=NULL WHERE id=?", (row_id,))
                    else:
                        try:
                            _safe_member("data/recordings/" + audio)
                        except BackupError:
                            conn.execute("UPDATE transcription_history SET audio_file=NULL WHERE id=?", (row_id,))
                        else:
                            rel = "recordings/" + audio
                            if (source_root / Path(rel)).is_file():
                                required_assets.add("data/" + rel)
                            else:
                                warnings.append(f"History entry {row_id} has no local audio file")
                                conn.execute("UPDATE transcription_history SET audio_file=NULL WHERE id=?", (row_id,))
        if _table(conn, "record_sync"):
            conn.execute("DELETE FROM record_sync")
        conn.commit()
        _quick_check(conn)
    return version, warnings, required_assets


def _merge_legacy_history(path: Path, source_root: Path) -> None:
    legacy = source_root / "transcription_history.json"
    if not legacy.exists():
        return
    if _is_link(legacy) or not legacy.is_file() or legacy.stat().st_size > MAX_MANIFEST_BYTES:
        raise BackupError("Legacy history JSON is unsafe or too large")
    with closing(sqlite3.connect(path)) as conn, conn:
        if not _table(conn, "transcription_history"):
            raise BackupError("Cannot merge legacy history into this database schema")
        if conn.execute("SELECT 1 FROM transcription_history LIMIT 1").fetchone():
            return  # matches DatabaseManager's first-run migration condition
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BackupError("Legacy history JSON is invalid") from exc
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise BackupError("Legacy history JSON has no valid entries list")
        allowed = ("id", "text", "raw_text", "timestamp", "model", "audio_file",
                   "transcription_time", "audio_duration", "file_size")
        columns = _columns(conn, "transcription_history")
        if not {"id", "text", "timestamp", "model"} <= columns:
            raise BackupError("Cannot merge legacy history into this database schema")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise BackupError("Legacy history entry is invalid")
            values = {key: entry.get(key) for key in allowed if key in columns}
            for key in ("text", "timestamp", "model"):
                values[key] = values.get(key) or ""
            placeholders = ",".join("?" for _ in values)
            conn.execute(
                f"INSERT OR REPLACE INTO transcription_history ({','.join(values)}) VALUES ({placeholders})",
                tuple(values.values()))


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    """Persist directory rename metadata where the platform supports it."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # Some filesystems do not support directory fsync.
    finally:
        os.close(fd)


def _read_json(path: Path, max_bytes: int = MAX_MANIFEST_BYTES) -> dict[str, Any]:
    if not path.is_file() or _is_link(path) or path.stat().st_size > max_bytes:
        raise BackupError("Restore metadata is missing or invalid")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError("Restore metadata is invalid") from exc
    if not isinstance(data, dict):
        raise BackupError("Restore metadata must be an object")
    return data


def _check_space(directory: Path, required: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free
    if free < required:
        raise BackupError("Not enough free disk space for a safe backup or restore")


def _source_signature(path: Path) -> tuple[int, int, int, int]:
    try:
        details = path.stat()
    except OSError as exc:
        raise BackupError(f"Source data became unavailable during backup: {path.name}") from exc
    if _is_link(path) or not stat.S_ISREG(details.st_mode):
        raise BackupError("A source file became a symbolic link or special file")
    return details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns


def _archive_file(zf: zipfile.ZipFile, name: str, source: Path,
                  cancel: Any) -> tuple[dict[str, Any], tuple[int, int, int, int]]:
    _safe_member(name)
    signature = _source_signature(source)
    if signature[2] > MAX_FILE_BYTES:
        raise BackupError("A source file is not regular or exceeds the size limit")
    info = zipfile.ZipInfo(name)
    info.compress_type = (zipfile.ZIP_STORED if source.suffix.lower() in
                          (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".mp4")
                          else zipfile.ZIP_DEFLATED)
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    digest = hashlib.sha256()
    count = 0
    with source.open("rb") as read, zf.open(info, "w", force_zip64=True) as write:
        opened = os.fstat(read.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
                signature[0], signature[1], signature[2]):
            raise BackupError(f"Data changed during backup: {name}")
        while block := read.read(CHUNK_SIZE):
            _check_cancel(cancel)
            write.write(block)
            digest.update(block)
            count += len(block)
    if _source_signature(source) != signature or count != signature[2]:
        raise BackupError(f"Data changed during backup: {name}")
    return {"path": name, "size": count, "sha256": digest.hexdigest()}, signature


def _manifest_info(path: Path, manifest: dict[str, Any]) -> BackupInfo:
    files = manifest["files"]
    return BackupInfo(
        path=path,
        created_at=manifest["created_at"],
        app_version=manifest["app_version"],
        schema_version=manifest["schema_version"],
        include_recordings=manifest["include_recordings"],
        file_count=len(files),
        total_bytes=sum(item["size"] for item in files),
        archive_bytes=path.stat().st_size,
        warnings=tuple(manifest.get("warnings", ())),
    )


def create_backup(destination: str | os.PathLike[str], *,
                  data_dir: str | os.PathLike[str] | None = None,
                  include_recordings: bool = True,
                  progress: Callable[[str, int, int], None] | None = None,
                  cancel: Any = None) -> BackupInfo:
    """Create an atomic ``.owbackup`` ZIP from a consistent SQLite snapshot.

    Call while there is no active meeting/recording.  A SQLite online backup
    makes the database snapshot consistent even if ordinary history reads are
    still occurring.  The output path is replaced only after full validation.
    """
    root = _root(data_dir)
    destination = Path(destination).expanduser().absolute()
    if destination.suffix.lower() != ARCHIVE_EXTENSION:
        raise BackupError(f"Backup file must end in {ARCHIVE_EXTENSION}")
    if any(_inside(destination, root / name) for name in
           (*RECORDING_NAMES, RESTORE_DIR)):
        raise BackupError("Choose a backup destination outside recorded data")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _check_cancel(cancel)
    with tempfile.TemporaryDirectory(prefix=".owbackup-build-", dir=destination.parent) as work_text:
        work = Path(work_text)
        db_copy = work / "openwhisper.db"
        schema = _snapshot_database(_safe_path(root, "openwhisper.db"), db_copy, cancel)
        _merge_legacy_history(db_copy, root)
        schema, warnings, required_assets = _sanitize_database(db_copy, root, include_recordings)
        files = _owned_files(root, include_recordings)
        if required_assets - {name for name, _ in files}:
            raise BackupError("A database-referenced recording changed during backup")
        if len(files) + 2 > MAX_FILES:
            raise BackupError("Too many files to archive")
        _progress(progress, "scan", len(files), len(files))
        settings_path = _safe_path(root, "openwhisper_settings.json")
        settings_signature = (_source_signature(settings_path)
                              if settings_path.exists() else None)
        source_settings = _read_settings(settings_path)
        if settings_signature is not None and _source_signature(settings_path) != settings_signature:
            raise BackupError("Settings changed during backup")
        settings = _sanitize_settings(source_settings)
        if source_settings.get("remote_records_location") in ("host", "both"):
            warnings.append("Host-kept-only records are not in this local archive")
        settings_copy = work / "openwhisper_settings.json"
        settings_copy.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        files = [("data/openwhisper_settings.json", settings_copy),
                 ("data/openwhisper.db", db_copy), *files]
        total = sum(path.stat().st_size for _, path in files)
        if total > MAX_TOTAL_BYTES:
            raise BackupError("Data exceeds backup size limit")
        _check_space(destination.parent, total + (32 << 20))
        temp_archive = work / ("bundle" + ARCHIVE_EXTENSION)
        records: list[dict[str, Any]] = []
        source_signatures: list[tuple[Path, tuple[int, int, int, int]]] = []
        with zipfile.ZipFile(temp_archive, "w", allowZip64=True) as zf:
            for index, (name, path) in enumerate(files, 1):
                _check_cancel(cancel)
                if not _inside(path, work):
                    _safe_path(root, name.removeprefix("data/"))
                record, signature = _archive_file(zf, name, path, cancel)
                records.append(record)
                if not _inside(path, work):
                    source_signatures.append((path, signature))
                _progress(progress, "archive", index, len(files))
            manifest = {
                "format": FORMAT,
                "format_version": FORMAT_VERSION,
                "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                "app_version": _app_version(),
                "schema_version": schema,
                "include_recordings": bool(include_recordings),
                "files": records,
                "warnings": warnings,
            }
            info = zipfile.ZipInfo(MANIFEST_NAME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            zf.writestr(info, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
        _check_cancel(cancel)
        _inspect_backup(temp_archive, cancel)  # verify exact bytes before publication
        _check_cancel(cancel)
        for path, signature in source_signatures:
            if _source_signature(path) != signature:
                raise BackupError(f"Data changed during backup: {path.name}")
        if ((settings_signature is None and settings_path.exists()) or
                (settings_signature is not None and
                 _source_signature(settings_path) != settings_signature)):
            raise BackupError("Settings changed during backup")
        with temp_archive.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_archive, destination)
        _fsync_directory(destination.parent)
    return _manifest_info(destination, manifest)


def _app_version() -> str:
    from _version import __version__

    return __version__


def _validate_manifest(zf: zipfile.ZipFile) -> dict[str, Any]:
    infos = zf.infolist()
    if len(infos) > MAX_FILES + 1:
        raise BackupError("Archive has too many members")
    seen: set[str] = set()
    members: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = _safe_member(info.filename)
        folded = name.casefold()
        if folded in seen or info.is_dir():
            raise BackupError("Archive contains duplicate names or directories")
        seen.add(folded)
        mode = info.external_attr >> 16
        if mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise BackupError("Archive contains a symbolic link or special file")
        if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
            raise BackupError("Archive member exceeds size limit")
        if info.file_size and (info.compress_size == 0 or
                               info.file_size / info.compress_size > MAX_RATIO):
            raise BackupError("Archive has an unsafe compression ratio")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise BackupError("Archive exceeds size limit")
        members[name] = info
    manifest_info = members.get(MANIFEST_NAME)
    if manifest_info is None or manifest_info.file_size > MAX_MANIFEST_BYTES:
        raise BackupError("Archive manifest is missing or too large")
    try:
        manifest = json.loads(zf.read(manifest_info))
    except (ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise BackupError("Archive manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError("Unsupported backup format")
    if not isinstance(manifest.get("created_at"), str) or not isinstance(manifest.get("app_version"), str):
        raise BackupError("Archive metadata is invalid")
    version = manifest.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0 or version > _current_schema():
        raise BackupError("Backup database schema is incompatible with this app")
    if not isinstance(manifest.get("include_recordings"), bool):
        raise BackupError("Archive recording flag is invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(infos) - 1:
        raise BackupError("Archive file list does not match its members")
    expected: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise BackupError("Archive file metadata is invalid")
        name = _safe_member(item.get("path"))
        size, digest = item.get("size"), item.get("sha256")
        if (name == MANIFEST_NAME or name in expected or name not in members or
                isinstance(size, bool) or not isinstance(size, int) or size < 0 or
                members[name].file_size != size or not isinstance(digest, str) or
                not _SHA256_RE.fullmatch(digest)):
            raise BackupError("Archive file metadata is invalid")
        expected.add(name)
        if name == "data/openwhisper_settings.json" and size > MAX_MANIFEST_BYTES:
            raise BackupError("Settings file is too large")
    if expected != set(members) - {MANIFEST_NAME}:
        raise BackupError("Archive contains unlisted files")
    if {"data/openwhisper.db", "data/openwhisper_settings.json"} - expected:
        raise BackupError("Archive is missing core data")
    if not manifest["include_recordings"] and any(
            name.split("/")[1] in RECORDING_NAMES for name in expected):
        raise BackupError("Text-only archive contains recording assets")
    warnings = manifest.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(w, str) or len(w) > 500 for w in warnings):
        raise BackupError("Archive warnings are invalid")
    return manifest


def _inspect_backup(path: str | os.PathLike[str], cancel: Any = None) -> BackupInfo:
    """Stream-verify every archive member, its manifest, and SQLite snapshot."""
    archive = Path(path).expanduser().absolute()
    if not archive.is_file():
        raise BackupError("Backup file does not exist")
    try:
        with tempfile.TemporaryDirectory(prefix=".owbackup-inspect-") as work_text:
            db_path = Path(work_text) / "openwhisper.db"
            with zipfile.ZipFile(archive, "r") as zf:
                manifest = _validate_manifest(zf)
                settings_bytes: bytearray | None = None
                for item in manifest["files"]:
                    _check_cancel(cancel)
                    digest = hashlib.sha256()
                    count = 0
                    settings_output = (bytearray() if item["path"] ==
                                       "data/openwhisper_settings.json" else None)
                    with zf.open(item["path"]) as handle:
                        output = db_path.open("wb") if item["path"] == "data/openwhisper.db" else None
                        try:
                            while block := handle.read(CHUNK_SIZE):
                                _check_cancel(cancel)
                                count += len(block)
                                if count > item["size"]:
                                    raise BackupError("Archive member exceeds declared size")
                                digest.update(block)
                                if output is not None:
                                    output.write(block)
                                if settings_output is not None:
                                    settings_output.extend(block)
                        finally:
                            if output is not None:
                                output.close()
                    if count != item["size"] or digest.hexdigest() != item["sha256"]:
                        raise BackupError(f"Archive checksum mismatch: {item['path']}")
                    if settings_output is not None:
                        settings_bytes = settings_output
                try:
                    settings_data = json.loads(settings_bytes)
                except (ValueError, TypeError) as exc:
                    raise BackupError("Backup settings JSON is invalid") from exc
                if not isinstance(settings_data, dict):
                    raise BackupError("Backup settings JSON must be an object")
            with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
                _check_cancel(cancel)
                _quick_check(conn)
                _check_cancel(cancel)
                if _db_schema(conn) != manifest["schema_version"] or _meeting_is_live(conn):
                    raise BackupError("Backup database metadata or meeting state is invalid")
                assets = {item["path"] for item in manifest["files"]}
                for table, column in (("meeting_sessions", "spool_dir"),
                                      ("meeting_audio_chunks", "file_path")):
                    if not _table(conn, table) or column not in _columns(conn, table):
                        continue
                    for (value,) in conn.execute(f"SELECT {column} FROM {table}"):
                        if not isinstance(value, str) or not value.startswith(PATH_MARKER):
                            raise BackupError("Backup database has an unsafe data path")
                        rel = value[len(PATH_MARKER):]
                        if not rel.startswith("meetings/"):
                            raise BackupError("Backup database has an unsafe meeting path")
                        _safe_member("data/" + rel)
                        if table == "meeting_audio_chunks" and "data/" + rel not in assets:
                            raise BackupError("Backup database references missing meeting audio")
                if (_table(conn, "transcription_history") and
                        "audio_file" in _columns(conn, "transcription_history")):
                    for (audio,) in conn.execute("SELECT audio_file FROM transcription_history WHERE audio_file IS NOT NULL"):
                        if not isinstance(audio, str) or Path(audio).is_absolute():
                            raise BackupError("Backup database has an unsafe recording path")
                        name = _safe_member("data/recordings/" + audio)
                        if name not in assets:
                            raise BackupError("Backup database references missing recording audio")
    except (OSError, EOFError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise BackupError("Backup ZIP is corrupt") from exc
    return _manifest_info(archive, manifest)


def inspect_backup(path: str | os.PathLike[str]) -> BackupInfo:
    """Validate checksums, paths, database references, and ZIP structure."""
    return _inspect_backup(path)


def _extract_verified(archive: Path, data_stage: Path, manifest: dict[str, Any],
                      progress: Callable[[str, int, int], None] | None,
                      cancel: Any) -> None:
    with zipfile.ZipFile(archive, "r") as zf:
        _validate_manifest(zf)
        for index, item in enumerate(manifest["files"], 1):
            _check_cancel(cancel)
            rel = item["path"].removeprefix("data/")
            output = _safe_path(data_stage, rel)
            output.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            count = 0
            with zf.open(item["path"]) as source, output.open("xb") as target:
                while block := source.read(CHUNK_SIZE):
                    _check_cancel(cancel)
                    count += len(block)
                    if count > item["size"]:
                        raise BackupError("Archive member exceeds declared size")
                    target.write(block)
                    digest.update(block)
                target.flush()
                os.fsync(target.fileno())
            if count != item["size"] or digest.hexdigest() != item["sha256"]:
                raise BackupError(f"Archive checksum mismatch: {item['path']}")
            _progress(progress, "stage", index, len(manifest["files"]))


def _rebase_database(path: Path, data_root: Path, expected_schema: int,
                     include_recordings: bool) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        _quick_check(conn)
        if _db_schema(conn) != expected_schema or _meeting_is_live(conn):
            raise BackupError("Backup database has incompatible or live meeting state")
        for table, column in (("meeting_sessions", "spool_dir"),
                              ("meeting_audio_chunks", "file_path")):
            if not _table(conn, table) or column not in _columns(conn, table):
                continue
            for row_id, value in conn.execute(f"SELECT id, {column} FROM {table}").fetchall():
                if not isinstance(value, str) or not value.startswith(PATH_MARKER):
                    raise BackupError("Backup database contains an unsafe data path")
                rel = value[len(PATH_MARKER):]
                if not rel.startswith("meetings/"):
                    raise BackupError("Backup database contains an unsafe meeting path")
                output = _safe_path(data_root, rel)
                conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (str(output), row_id))
        if not include_recordings and _table(conn, "meeting_audio_chunks"):
            if conn.execute("SELECT 1 FROM meeting_audio_chunks LIMIT 1").fetchone():
                raise BackupError("Text-only backup still contains audio chunks")
        conn.commit()
        _quick_check(conn)


def _restore_home(root: Path) -> Path:
    return _safe_path(root, RESTORE_DIR) if RESTORE_DIR in OWNED_NAMES else root / RESTORE_DIR


def pending_restore_exists(data_dir: str | os.PathLike[str] | None = None) -> bool:
    root = _root(data_dir)
    home = _restore_home(root)
    if _is_link(home):
        raise BackupError("Restore directory is a symbolic link")
    return (home / PENDING_NAME).exists() or (home / JOURNAL_NAME).exists()


def previous_data_dir(data_dir: str | os.PathLike[str] | None = None) -> Path | None:
    root = _root(data_dir)
    path = _restore_home(root) / LAST_NAME
    if not path.is_file():
        return None
    data = _read_json(path)
    name = data.get("previous")
    if not isinstance(name, str) or not re.fullmatch(r"previous-[A-Za-z0-9_-]+", name):
        raise BackupError("Restore history metadata is invalid")
    return _restore_home(root) / name


def prepare_restore(path: str | os.PathLike[str], *,
                    data_dir: str | os.PathLike[str] | None = None,
                    progress: Callable[[str, int, int], None] | None = None,
                    cancel: Any = None) -> BackupInfo:
    """Validate and stage a backup; next startup applies it offline."""
    root = _root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    if _is_link(root):
        raise BackupError("The data directory is a symbolic link")
    archive = Path(path).expanduser().absolute()
    info = _inspect_backup(archive, cancel)
    _check_cancel(cancel)
    home = _restore_home(root)
    home.mkdir(parents=True, exist_ok=True)
    if _is_link(home) or (home / JOURNAL_NAME).exists():
        raise BackupError("An earlier restore requires recovery before staging another")
    if (home / PENDING_NAME).exists():
        raise BackupError("A restore is already waiting for restart")
    current_db = root / "openwhisper.db"
    if current_db.is_file():
        with closing(sqlite3.connect(current_db.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            if _meeting_is_live(conn):
                raise BackupError("End the live meeting before preparing a restore")
    _check_space(root, info.total_bytes + (32 << 20))
    stage_name = "stage-" + uuid.uuid4().hex
    stage = home / stage_name
    stage.mkdir()
    try:
        with zipfile.ZipFile(archive) as zf:
            manifest = _validate_manifest(zf)
        data_stage = stage / "data"
        data_stage.mkdir()
        _extract_verified(archive, data_stage, manifest, progress, cancel)
        _rebase_database(data_stage / "openwhisper.db", root,
                         info.schema_version, info.include_recordings)
        # A valid checksummed ZIP may have been authored by someone else.
        # Apply the portable-import policy here as well as during export.
        settings_path = data_stage / "openwhisper_settings.json"
        safe_settings = _sanitize_settings(_read_settings(settings_path))
        settings_path.write_text(json.dumps(safe_settings, ensure_ascii=False,
                                            indent=2) + "\n", encoding="utf-8")
        _check_cancel(cancel)
        db_path = data_stage / "openwhisper.db"
        manifest["staged_db_sha256"] = _file_sha256(db_path)
        manifest["staged_db_size"] = db_path.stat().st_size
        manifest["staged_settings_sha256"] = _file_sha256(settings_path)
        manifest["staged_settings_size"] = settings_path.stat().st_size
        _write_json_atomic(stage / MANIFEST_NAME, manifest)
        _write_json_atomic(home / PENDING_NAME, {"stage": stage_name,
                                                  "created_at": info.created_at})
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return info


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(CHUNK_SIZE):
            digest.update(block)
    return digest.hexdigest()


def _load_pending(home: Path) -> tuple[str, Path]:
    pending = _read_json(home / PENDING_NAME)
    name = pending.get("stage")
    if not isinstance(name, str) or not re.fullmatch(r"stage-[0-9a-f]{32}", name):
        raise BackupError("Pending restore metadata is unsafe")
    stage = home / name
    if not stage.is_dir() or _is_link(stage):
        raise BackupError("Pending restore stage is missing")
    return name, stage


def _restore_stage_check(stage: Path, root: Path) -> None:
    manifest = _read_json(stage / MANIFEST_NAME)
    if (manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION
            or not isinstance(manifest.get("schema_version"), int)
            or not isinstance(manifest.get("include_recordings"), bool)):
        raise BackupError("Staged restore manifest is incompatible")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise BackupError("Staged restore manifest is invalid")
    if _is_link(stage / "data"):
        raise BackupError("Staged data directory is a symbolic link")
    listed: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise BackupError("Staged restore manifest is invalid")
        name = _safe_member(item.get("path"))
        if name == MANIFEST_NAME:
            raise BackupError("Staged restore manifest is invalid")
        if name in listed:
            raise BackupError("Staged restore has duplicate entries")
        listed.add(name)
        source = _safe_path(stage / "data", name.removeprefix("data/"))
        expected_size = (manifest.get("staged_db_size") if name == "data/openwhisper.db"
                         else manifest.get("staged_settings_size") if name == "data/openwhisper_settings.json"
                         else item.get("size"))
        if (isinstance(expected_size, bool) or not isinstance(expected_size, int)
                or expected_size < 0 or not source.is_file()
                or source.stat().st_size != expected_size):
            raise BackupError("Staged restore file is missing or changed")
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            while block := handle.read(CHUNK_SIZE):
                digest.update(block)
        expected_hash = (manifest.get("staged_db_sha256") if name == "data/openwhisper.db"
                         else manifest.get("staged_settings_sha256") if name == "data/openwhisper_settings.json"
                         else item.get("sha256"))
        if digest.hexdigest() != expected_hash:
            raise BackupError("Staged restore checksum mismatch")
    found: set[str] = set()
    data_stage = stage / "data"
    for folder, dirs, names in os.walk(data_stage, followlinks=False):
        folder_path = Path(folder)
        if any(_is_link(folder_path / directory) for directory in dirs):
            raise BackupError("Staged restore has a symbolic link")
        for filename in names:
            path = folder_path / filename
            if _is_link(path) or not stat.S_ISREG(path.stat().st_mode):
                raise BackupError("Staged restore has a special file")
            found.add("data/" + path.relative_to(data_stage).as_posix())
    if found != listed:
        raise BackupError("Staged restore contains unlisted data")
    with closing(sqlite3.connect(stage / "data" / "openwhisper.db")) as conn:
        _quick_check(conn)
        if _db_schema(conn) != manifest["schema_version"] or _meeting_is_live(conn):
            raise BackupError("Staged database metadata is invalid")
        for table, column in (("meeting_sessions", "spool_dir"),
                              ("meeting_audio_chunks", "file_path")):
            if not _table(conn, table) or column not in _columns(conn, table):
                continue
            for (value,) in conn.execute(f"SELECT {column} FROM {table}"):
                if not isinstance(value, str):
                    raise BackupError("Staged database has an unsafe path")
                try:
                    relative = Path(value).absolute().relative_to(root.absolute()).as_posix()
                except ValueError as exc:
                    raise BackupError("Staged database path escapes data root") from exc
                if not relative.startswith("meetings/"):
                    raise BackupError("Staged database path is outside meetings")
                _safe_member("data/" + relative)
                if table == "meeting_audio_chunks" and "data/" + relative not in found:
                    raise BackupError("Staged database references missing meeting audio")
        if (_table(conn, "transcription_history") and
                "audio_file" in _columns(conn, "transcription_history")):
            for (audio,) in conn.execute("SELECT audio_file FROM transcription_history WHERE audio_file IS NOT NULL"):
                if not isinstance(audio, str) or Path(audio).is_absolute():
                    raise BackupError("Staged database has an unsafe recording path")
                name = _safe_member("data/recordings/" + audio)
                if name not in found:
                    raise BackupError("Staged database references missing recording audio")


def _move_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    _fsync_directory(destination.parent)
    if source.parent != destination.parent:
        _fsync_directory(source.parent)


def _rollback(root: Path, stage: Path, previous: Path,
              stage_names: set[str]) -> None:
    """Reverse any partial installation using existence, not journal offsets."""
    for name in reversed(RESTORE_NAMES):
        target = _safe_path(root, name)
        staged = _safe_path(stage / "data", name)
        old = _safe_path(previous, name)
        if old.exists():
            if target.exists():
                if staged.exists():
                    raise BackupError("Cannot roll back: both new copies exist")
                _move_path(target, staged)
            _move_path(old, target)
        elif name in stage_names and not staged.exists() and target.exists():
            # The target had no prior file and the stage item was installed.
            _move_path(target, staged)


def apply_pending_restore(*, data_dir: str | os.PathLike[str] | None = None) -> bool:
    """Apply a staged restore before initializing settings or SQLite.

    On failure, prior owned data is rolled back and the stage remains for a
    later retry.  After success, prior data is retained in a dated directory.
    A journal allows a crash between renames to be recovered on next launch.
    """
    root = _root(data_dir)
    home = _restore_home(root)
    journal_path = home / JOURNAL_NAME
    pending_path = home / PENDING_NAME
    if not pending_path.exists() and not journal_path.exists():
        return False
    if _is_link(home):
        raise BackupError("Restore directory is a symbolic link")
    if journal_path.exists():
        journal = _read_json(journal_path)
        stage_name = journal.get("stage")
        previous_name = journal.get("previous")
        if (not isinstance(stage_name, str) or not re.fullmatch(r"stage-[0-9a-f]{32}", stage_name)
                or not isinstance(previous_name, str) or
                not re.fullmatch(r"previous-[A-Za-z0-9_-]+", previous_name)):
            raise BackupError("Restore journal is unsafe")
        raw_names = journal.get("stage_names")
        if (not isinstance(raw_names, list) or any(name not in RESTORE_NAMES for name in raw_names)
                or len(set(raw_names)) != len(raw_names)):
            raise BackupError("Restore journal has unsafe path metadata")
        stage_names = set(raw_names)
        stage, previous = home / stage_name, home / previous_name
        if journal.get("phase") == "committed":
            _write_json_atomic(home / LAST_NAME, {"previous": previous_name})
            pending_path.unlink(missing_ok=True)
            journal_path.unlink(missing_ok=True)
            return True
        if journal.get("phase") != "applying" or not stage.is_dir() or not previous.is_dir():
            raise BackupError("Restore journal cannot be recovered automatically")
        _rollback(root, stage, previous, stage_names)
        journal_path.unlink()
        # A complete stage is now available again; continue with a fresh pass.
    if not pending_path.exists():
        return False
    stage_name, stage = _load_pending(home)
    _restore_stage_check(stage, root)
    previous_name = "previous-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    previous = home / previous_name
    previous.mkdir()
    _fsync_directory(home)
    stage_names = {name for name in RESTORE_NAMES if (stage / "data" / name).exists()}
    _write_json_atomic(journal_path, {"phase": "applying", "stage": stage_name,
                                      "previous": previous_name,
                                      "stage_names": sorted(stage_names)})
    try:
        for name in RESTORE_NAMES:
            target = _safe_path(root, name)
            staged = _safe_path(stage / "data", name)
            old = _safe_path(previous, name)
            if target.exists():
                _move_path(target, old)
            if staged.exists():
                _move_path(staged, target)
        _write_json_atomic(journal_path, {"phase": "committed", "stage": stage_name,
                                          "previous": previous_name,
                                          "stage_names": sorted(stage_names)})
    except BaseException:
        _rollback(root, stage, previous, stage_names)
        journal_path.unlink(missing_ok=True)
        raise
    _write_json_atomic(home / LAST_NAME, {"previous": previous_name})
    pending_path.unlink(missing_ok=True)
    journal_path.unlink(missing_ok=True)
    shutil.rmtree(stage, ignore_errors=True)
    return True

