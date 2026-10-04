"""Safety and portability checks for the offline backup service."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from services import backup


def _make_data(root: Path, *, live: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "recordings").mkdir()
    (root / "recordings" / "own.wav").write_bytes(b"RIFF" + b"a" * 200)
    (root / "recordings" / "devices" / "peer").mkdir(parents=True)
    (root / "recordings" / "devices" / "peer" / "remote.wav").write_bytes(b"RIFFremote")
    (root / "meetings" / "m1").mkdir(parents=True)
    chunk = root / "meetings" / "m1" / "mic-000.wav"
    chunk.write_bytes(b"RIFF" + b"b" * 256)
    (root / "remote_records" / "cache").mkdir(parents=True)
    (root / "remote_records" / "cache" / "throwaway.wav").write_bytes(b"cached")
    (root / "remote_records" / "retained").mkdir()
    (root / "remote_records" / "retained" / "note.json").write_text("{}")
    (root / "recorded_audio.wav").write_bytes(b"RIFFrecovery")
    (root / ".env").write_text("OPENAI_API_KEY=do-not-export")
    (root / "remote_engine_host_key.pem").write_text("private-key")
    settings = {
        "ui_theme": "dark",
        "remote_engine_client": {"host": "elsewhere", "fingerprint": "old"},
        "remote_host_enabled": True,
        "remote_host_devices": [{"token_hash": "old-token-digest"}],
        "remote_records_location": "host",
        "mcp_enabled": True,
        "meeting_context_folder_path": str(root / "external"),
        "legacy_secret": "do-not-export",
    }
    (root / "openwhisper_settings.json").write_text(json.dumps(settings), encoding="utf-8")
    with closing(sqlite3.connect(root / "openwhisper.db")) as conn, conn:
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            CREATE TABLE transcription_history (
                id TEXT PRIMARY KEY, audio_file TEXT);
            CREATE TABLE meeting_sessions (
                id TEXT PRIMARY KEY, status TEXT, spool_dir TEXT,
                host_token TEXT, guest_token TEXT, asr_remote_json TEXT,
                app_pid INTEGER, app_heartbeat_at TEXT);
            CREATE TABLE meeting_audio_chunks (
                id INTEGER PRIMARY KEY, file_path TEXT);
            CREATE TABLE meeting_segments (
                id TEXT PRIMARY KEY, chunk_id INTEGER);
            CREATE TABLE record_sync (kind TEXT, record_id TEXT);
        """)
        conn.execute("INSERT INTO schema_version VALUES (?)", (backup._current_schema(),))
        conn.execute("INSERT INTO transcription_history VALUES ('h1','own.wav')")
        conn.execute("INSERT INTO transcription_history VALUES ('h2','devices/peer/remote.wav')")
        conn.execute("INSERT INTO meeting_sessions VALUES (?,?,?,?,?,?,?,?)", (
            "m1", "active" if live else "ended", str(root / "meetings" / "m1"),
            "old-host-token", "old-guest-token", '{"host":"old"}', 123, "yesterday"))
        conn.execute("INSERT INTO meeting_audio_chunks VALUES (?,?)", (1, str(chunk)))
        conn.execute("INSERT INTO meeting_segments VALUES ('s1',1)")
        conn.execute("INSERT INTO record_sync VALUES ('meeting','m1')")


def test_backup_restores_to_a_different_root_and_keeps_previous_data(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    _make_data(source)
    _make_data(target)
    (target / "recordings" / "prior-only.wav").write_bytes(b"previous")
    archive = tmp_path / "portable.owbackup"

    info = backup.create_backup(archive, data_dir=source)
    assert info == backup.inspect_backup(archive)
    assert info.include_recordings and info.file_count >= 6
    assert any("Host-kept-only" in warning for warning in info.warnings)
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "data/recordings/own.wav" in names
        assert not any(name.startswith("data/remote_records/") for name in names)
        assert ".env" not in "\n".join(names)
        assert "remote_engine_host_key.pem" not in "\n".join(names)
        settings = json.loads(zf.read("data/openwhisper_settings.json"))
        assert settings["ui_theme"] == "dark"
        assert settings["mcp_enabled"] is False
        assert settings["remote_records_location"] == "local"
        assert "remote_engine_client" not in settings
        assert "remote_host_devices" not in settings
        assert "legacy_secret" not in settings

    backup.prepare_restore(archive, data_dir=target)
    assert backup.pending_restore_exists(target)
    assert backup.apply_pending_restore(data_dir=target)
    assert not backup.pending_restore_exists(target)
    assert (target / "recordings" / "own.wav").read_bytes() == b"RIFF" + b"a" * 200
    assert not (target / "recordings" / "prior-only.wav").exists()
    previous = backup.previous_data_dir(target)
    assert previous is not None
    assert (previous / "recordings" / "prior-only.wav").read_bytes() == b"previous"
    assert (target / ".env").read_text() == "OPENAI_API_KEY=do-not-export"
    with closing(sqlite3.connect(target / "openwhisper.db")) as conn:
        meeting = conn.execute("SELECT spool_dir,host_token,guest_token,asr_remote_json,app_pid FROM meeting_sessions").fetchone()
        assert meeting[0] == str(target / "meetings" / "m1")
        assert meeting[1] != "old-host-token" and meeting[2] != "old-guest-token"
        assert meeting[3:] == (None, None)
        assert conn.execute("SELECT file_path FROM meeting_audio_chunks").fetchone()[0] == str(target / "meetings" / "m1" / "mic-000.wav")
        assert conn.execute("SELECT count(*) FROM record_sync").fetchone()[0] == 0
    with closing(sqlite3.connect(source / "openwhisper.db")) as conn:
        assert conn.execute("SELECT host_token FROM meeting_sessions").fetchone()[0] == "old-host-token"


def test_text_only_archive_clears_audio_references(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    _make_data(source)
    target.mkdir()
    archive = tmp_path / "text.owbackup"
    info = backup.create_backup(archive, data_dir=source, include_recordings=False)
    assert not info.include_recordings
    with zipfile.ZipFile(archive) as zf:
        assert not any(name.startswith("data/recordings/") for name in zf.namelist())
    backup.prepare_restore(archive, data_dir=target)
    assert backup.apply_pending_restore(data_dir=target)
    with closing(sqlite3.connect(target / "openwhisper.db")) as conn:
        assert conn.execute("SELECT audio_file FROM transcription_history WHERE id='h1'").fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM meeting_audio_chunks").fetchone()[0] == 0
        assert conn.execute("SELECT chunk_id FROM meeting_segments").fetchone()[0] is None


def test_live_meeting_is_refused_and_existing_output_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_data(source, live=True)
    archive = tmp_path / "existing.owbackup"
    archive.write_bytes(b"old")
    with pytest.raises(backup.BackupError, match="live meeting"):
        backup.create_backup(archive, data_dir=source)
    assert archive.read_bytes() == b"old"


def test_cancel_does_not_publish_a_partial_archive(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_data(source)
    archive = tmp_path / "existing.owbackup"
    archive.write_bytes(b"old")
    with pytest.raises(backup.BackupCancelled):
        backup.create_backup(archive, data_dir=source, cancel=lambda: True)
    assert archive.read_bytes() == b"old"


def test_destination_inside_not_yet_created_recordings_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    archive = source / "recordings" / "first.owbackup"
    with pytest.raises(backup.BackupError, match="outside recorded data"):
        backup.create_backup(archive, data_dir=source)
    assert not archive.parent.exists()


def test_corrupt_and_traversal_archives_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_data(source)
    archive = tmp_path / "good.owbackup"
    backup.create_backup(archive, data_dir=source)
    corrupt = tmp_path / "corrupt.owbackup"
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(corrupt, "w") as output:
        for item in original.infolist():
            data = original.read(item)
            if item.filename == "data/recordings/own.wav":
                data += b"changed"
            output.writestr(item, data)
    with pytest.raises(backup.BackupError, match="metadata|checksum"):
        backup.inspect_backup(corrupt)

    traversal = tmp_path / "traversal.owbackup"
    with zipfile.ZipFile(traversal, "w") as output:
        output.writestr("data/../outside", b"evil")
        output.writestr("manifest.json", b"{}")
    with pytest.raises(backup.BackupError, match="Unsafe"):
        backup.inspect_backup(traversal)

    link_archive = tmp_path / "symlink.owbackup"
    with zipfile.ZipFile(link_archive, "w") as output:
        info = zipfile.ZipInfo("data/recordings/link.wav")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(info, b"../../somewhere")
        output.writestr("manifest.json", b"{}")
    with pytest.raises(backup.BackupError, match="symbolic link"):
        backup.inspect_backup(link_archive)


def test_checksums_cannot_hide_missing_database_audio(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_data(source)
    archive = tmp_path / "good.owbackup"
    backup.create_backup(archive, data_dir=source)
    crafted = tmp_path / "missing-audio.owbackup"
    missing = "data/meetings/m1/mic-000.wav"
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(crafted, "w") as output:
        manifest = json.loads(original.read("manifest.json"))
        manifest["files"] = [item for item in manifest["files"]
                             if item["path"] != missing]
        for info in original.infolist():
            if info.filename == missing:
                continue
            payload = (json.dumps(manifest).encode() if info.filename == "manifest.json"
                       else original.read(info))
            output.writestr(info, payload)
    with pytest.raises(backup.BackupError, match="missing meeting audio"):
        backup.inspect_backup(crafted)


def test_failed_apply_rolls_back_entire_owned_set_and_can_retry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    _make_data(source)
    _make_data(target)
    (target / "recordings" / "prior-only.wav").write_bytes(b"prior")
    old_settings = (target / "openwhisper_settings.json").read_bytes()
    archive = tmp_path / "good.owbackup"
    backup.create_backup(archive, data_dir=source)
    backup.prepare_restore(archive, data_dir=target)
    original_replace = backup.os.replace
    failed = False

    def fail_once(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
        nonlocal failed
        if (not failed and Path(src).name == "openwhisper.db" and
                Path(src).parent.name == "data" and Path(dst) == target / "openwhisper.db"):
            failed = True
            raise OSError("injected install failure")
        original_replace(src, dst)

    monkeypatch.setattr(backup.os, "replace", fail_once)
    with pytest.raises(OSError, match="injected"):
        backup.apply_pending_restore(data_dir=target)
    assert (target / "openwhisper_settings.json").read_bytes() == old_settings
    assert (target / "recordings" / "prior-only.wav").read_bytes() == b"prior"
    assert backup.pending_restore_exists(target)
    monkeypatch.setattr(backup.os, "replace", original_replace)
    assert backup.apply_pending_restore(data_dir=target)


def test_rebased_database_can_grow_and_import_rescrubs_settings(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / ("long-destination-" + "x" * 65)
    _make_data(source)
    with closing(sqlite3.connect(source / "openwhisper.db")) as conn, conn:
        chunk = str(source / "meetings" / "m1" / "mic-000.wav")
        conn.executemany("INSERT INTO meeting_audio_chunks VALUES (?,?)",
                         ((i, chunk) for i in range(2, 800)))
    archive = tmp_path / "portable.owbackup"
    backup.create_backup(archive, data_dir=source)
    crafted = tmp_path / "crafted.owbackup"
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(crafted, "w") as output:
        manifest = json.loads(original.read("manifest.json"))
        settings = json.loads(original.read("data/openwhisper_settings.json"))
        settings.update({"mcp_enabled": True, "remote_host_enabled": True,
                         "remote_host_devices": [{"token_hash": "stale"}],
                         "hidden_secret": "do-not-import",
                         "text_llm_profiles": [{"base_url":
                             "https://person:password@example.com/v1?token=secret#fragment"}]})
        settings_bytes = json.dumps(settings).encode()
        for item in manifest["files"]:
            if item["path"] == "data/openwhisper_settings.json":
                item["size"] = len(settings_bytes)
                item["sha256"] = hashlib.sha256(settings_bytes).hexdigest()
        for info in original.infolist():
            if info.filename == "manifest.json":
                output.writestr(info, json.dumps(manifest).encode())
            elif info.filename == "data/openwhisper_settings.json":
                output.writestr(info, settings_bytes)
            else:
                output.writestr(info, original.read(info))
    backup.prepare_restore(crafted, data_dir=target)
    pending = json.loads((target / backup.RESTORE_DIR / backup.PENDING_NAME).read_text())
    staged_db = target / backup.RESTORE_DIR / pending["stage"] / "data" / "openwhisper.db"
    archived_db_size = next(item["size"] for item in manifest["files"]
                            if item["path"] == "data/openwhisper.db")
    assert staged_db.stat().st_size > archived_db_size
    assert backup.apply_pending_restore(data_dir=target)
    restored = json.loads((target / "openwhisper_settings.json").read_text())
    assert restored["mcp_enabled"] is False
    assert restored["remote_host_enabled"] is False
    assert "remote_host_devices" not in restored
    assert "hidden_secret" not in restored
    assert restored["text_llm_profiles"][0]["base_url"] == "https://example.com/v1"


def test_restore_moves_stale_sqlite_sidecars_and_recovers_committed_journal(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    _make_data(source)
    _make_data(target)
    archive = tmp_path / "portable.owbackup"
    backup.create_backup(archive, data_dir=source)
    backup.prepare_restore(archive, data_dir=target)
    # Capture real SQLite WAL/SHM bytes before close, then put them back to
    # model a crash after staging. The next startup must remove their replay
    # path from the imported database.
    with closing(sqlite3.connect(target / "openwhisper.db")) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("UPDATE meeting_sessions SET host_token='old-wal-token'")
        conn.commit()
        wal_bytes = (target / "openwhisper.db-wal").read_bytes()
        shm_bytes = (target / "openwhisper.db-shm").read_bytes()
    assert len(wal_bytes) > 32 and len(shm_bytes) > 32
    (target / "openwhisper.db-wal").write_bytes(wal_bytes)
    (target / "openwhisper.db-shm").write_bytes(shm_bytes)
    (target / "openwhisper.db-journal").write_bytes(b"old-rollback-journal")
    journal_path = target / backup.RESTORE_DIR / backup.JOURNAL_NAME
    original_unlink = Path.unlink
    failed = False

    def fail_journal_unlink(self: Path, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if self == journal_path and not failed:
            failed = True
            raise OSError("injected committed-journal interruption")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
    with pytest.raises(OSError, match="committed-journal"):
        backup.apply_pending_restore(data_dir=target)
    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert not (target / backup.RESTORE_DIR / backup.PENDING_NAME).exists()
    assert journal_path.exists()
    assert backup.pending_restore_exists(target)
    assert backup.apply_pending_restore(data_dir=target)
    previous = backup.previous_data_dir(target)
    assert previous is not None
    for suffix, expected in (("-wal", wal_bytes), ("-shm", shm_bytes),
                             ("-journal", b"old-rollback-journal")):
        assert not (target / ("openwhisper.db" + suffix)).exists()
        assert (previous / ("openwhisper.db" + suffix)).read_bytes() == expected
    with closing(sqlite3.connect(target / "openwhisper.db")) as conn:
        assert conn.execute("SELECT host_token FROM meeting_sessions").fetchone()[0] != "old-wal-token"


@pytest.mark.parametrize("include_recordings", [True, False])
def test_real_database_manager_wal_roundtrip(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        include_recordings: bool) -> None:
    from sqlalchemy import text as sql_text

    from meeting.persist.repository import SqlMeetingRepository
    from services.database import DatabaseManager, config
    from services.models import MeetingAudioChunk, MeetingSegment

    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "recordings").mkdir()
    (source / "recordings" / "dictation.wav").write_bytes(b"RIFFhistory")
    spool = source / "meetings" / "meeting-real"
    spool.mkdir(parents=True)
    chunk_path = spool / "mic-000.wav"
    chunk_path.write_bytes(b"RIFFmeeting")
    monkeypatch.setattr(config, "HISTORY_FILE", str(source / "transcription_history.json"))
    source_db = DatabaseManager(db_path=str(source / "openwhisper.db"))
    try:
        source_db.add_history_entry("history-real", "dictation text",
                                    "2026-10-04T00:00:00Z", "local",
                                    audio_file="dictation.wav")
        repo = SqlMeetingRepository(source_db)
        repo.create_meeting(id="meeting-real", title="Real meeting", status="ended",
                            started_at="2026-10-04T00:00:00Z", ended_at="2026-10-04T00:01:00Z",
                            host_token="old-host", guest_token="old-guest", spool_dir=str(spool))
        with source_db.get_session() as session:
            chunk = MeetingAudioChunk(meeting_id="meeting-real", channel="mic", seq=0,
                                      file_path=str(chunk_path), start_s=0.0,
                                      duration_s=1.0, sample_rate=16000)
            session.add(chunk)
            session.flush()
            session.add(MeetingSegment(id="segment-real", meeting_id="meeting-real",
                                       chunk_id=chunk.id, channel="mic", start_s=0.0,
                                       end_s=1.0, text="hello", created_at="2026-10-04T00:00:00Z"))
        assert (source / "openwhisper.db-wal").exists()
        archive = tmp_path / ("real-full.owbackup" if include_recordings
                              else "real-text.owbackup")
        backup.create_backup(archive, data_dir=source,
                             include_recordings=include_recordings)
    finally:
        source_db.close()
    backup.prepare_restore(archive, data_dir=target)
    assert backup.apply_pending_restore(data_dir=target)
    monkeypatch.setattr(config, "HISTORY_FILE", str(target / "transcription_history.json"))
    target_db = DatabaseManager(db_path=str(target / "openwhisper.db"))
    try:
        repo = SqlMeetingRepository(target_db)
        meeting = repo.get_meeting("meeting-real")
        assert meeting is not None and meeting["title"] == "Real meeting"
        assert meeting["spool_dir"] == str(target / "meetings" / "meeting-real")
        with target_db.get_session() as session:
            history = session.execute(sql_text(
                "SELECT text,audio_file FROM transcription_history WHERE id='history-real'"
            )).fetchone()
            assert history == ("dictation text", "dictation.wav" if include_recordings else None)
            chunks = session.query(MeetingAudioChunk).all()
            segment = session.query(MeetingSegment).one()
            assert len(chunks) == int(include_recordings)
            assert segment.chunk_id == (chunks[0].id if include_recordings else None)
            if include_recordings:
                assert chunks[0].file_path == str(target / "meetings" / "meeting-real" / "mic-000.wav")
    finally:
        target_db.close()


def test_legacy_history_is_imported_into_snapshot_without_raw_json(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import text as sql_text

    from services.database import DatabaseManager, config

    source, target = tmp_path / "legacy", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "recordings").mkdir()
    (source / "recordings" / "old.wav").write_bytes(b"RIFFold")
    (source / "transcription_history.json").write_text(json.dumps({"entries": [
        {"id": "legacy-id", "text": "from JSON", "timestamp": "now",
         "model": "local", "audio_file": "old.wav"}]}))
    (target / "transcription_history.json").write_text(json.dumps({"entries": [
        {"id": "stale", "text": "stale", "timestamp": "old",
         "model": "local", "audio_file": "C:/missing/secret.wav"}]}))
    archive = tmp_path / "legacy.owbackup"
    backup.create_backup(archive, data_dir=source)
    with zipfile.ZipFile(archive) as zf:
        assert "data/transcription_history.json" not in zf.namelist()
    backup.prepare_restore(archive, data_dir=target)
    assert backup.apply_pending_restore(data_dir=target)
    assert not (target / "transcription_history.json").exists()
    monkeypatch.setattr(config, "HISTORY_FILE", str(target / "transcription_history.json"))
    db = DatabaseManager(db_path=str(target / "openwhisper.db"))
    try:
        with db.get_session() as session:
            rows = session.execute(sql_text(
                "SELECT id,text,audio_file FROM transcription_history")).fetchall()
            assert rows == [("legacy-id", "from JSON", "old.wav")]
    finally:
        db.close()

