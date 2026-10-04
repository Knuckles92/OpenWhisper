"""Coordinator tests use a fake archive service and temporary data only."""

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services.backup import BackupCancelled, BackupInfo
from services.backup_manager import BackupCoordinator, PREFERENCES_FILE


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


def _wait_for(predicate, timeout=3):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    app.processEvents()
    assert predicate(), "Timed out waiting for the backup worker"


def _info(path):
    path = Path(path)
    return BackupInfo(path=path, created_at="2026-10-04T12:00:00+00:00",
                      app_version="2.5.0", schema_version=1,
                      include_recordings=True, file_count=2,
                      total_bytes=20, archive_bytes=path.stat().st_size)


class WritingService:
    def __init__(self):
        self.calls = []

    def create_backup(self, path, *, data_dir, include_recordings, progress, cancel):
        self.calls.append(("backup", str(path), include_recordings))
        assert not cancel.is_set()
        Path(path).write_bytes(b"archive")
        progress("Writing archive", 7, 7)
        return _info(path)

    def inspect_backup(self, path):
        self.calls.append(("inspect", str(path)))
        return _info(path)

    def prepare_restore(self, path, *, data_dir):
        self.calls.append(("restore", str(path)))
        return _info(path)


def test_worker_completion_releases_reservation_after_context_exit(tmp_path):
    events = []
    started = threading.Event()
    finish = threading.Event()

    @contextmanager
    def worker_context():
        events.append("context_enter")
        try:
            yield
        finally:
            events.append("context_exit")

    class SlowService(WritingService):
        def create_backup(self, path, **kwargs):
            started.set()
            assert finish.wait(3)
            return super().create_backup(path, **kwargs)

    service = SlowService()
    manager = BackupCoordinator(data_dir=tmp_path / "data", service=service,
                                acquire=lambda: events.append("acquire"),
                                release=lambda: events.append("release"),
                                worker_context=worker_context)
    manager.busy_changed.connect(lambda busy: events.append("busy" if busy else "idle"))
    manager.backup_finished.connect(lambda info, error: events.append("finished"))
    try:
        destination = tmp_path / "manual.owbackup"
        manager.create_backup(destination, include_recordings=False)
        assert started.wait(2)
        assert manager.busy
        assert events[0:2] == ["acquire", "busy"]
        assert "release" not in events
        finish.set()
        _wait_for(lambda: "finished" in events)
        assert service.calls == [("backup", str(destination), False)]
        assert events.index("context_exit") < events.index("release")
        assert events.index("release") < events.index("idle") < events.index("finished")
        assert not manager.busy
        assert manager.snapshot()["last_backup"]["path"] == str(destination)
    finally:
        finish.set()
        manager.shutdown()


def test_cancelled_backup_reports_error_and_releases_reservation(tmp_path):
    started = threading.Event()
    releases = []
    results = []

    class CancelService(WritingService):
        def create_backup(self, path, *, cancel, **kwargs):
            started.set()
            assert cancel.wait(3)
            raise BackupCancelled("Backup canceled")

    manager = BackupCoordinator(data_dir=tmp_path / "data", service=CancelService(),
                                acquire=lambda: None, release=lambda: releases.append(True))
    manager.backup_finished.connect(lambda info, error: results.append((info, error)))
    try:
        destination = tmp_path / "cancelled.owbackup"
        manager.create_backup(destination)
        assert started.wait(2)
        manager.cancel()
        _wait_for(lambda: bool(results))
        assert results == [(None, "Backup canceled")]
        assert releases == [True]
        assert not destination.exists()
        assert not manager.busy
    finally:
        manager.shutdown()


def test_schedule_is_off_by_default_and_persists_explicit_choice(tmp_path):
    data = tmp_path / "data"
    folder = tmp_path / "backups"
    folder.mkdir()
    service = WritingService()
    manager = BackupCoordinator(data_dir=data, service=service, clock=lambda: 86401)
    try:
        assert manager.snapshot()["frequency"] == "off"
        assert manager.snapshot()["next_backup_at"] is None
        manager._scheduled_tick()
        assert service.calls == []
        manager.set_schedule("weekly", 3, str(folder))
        assert manager.snapshot()["frequency"] == "weekly"
        assert (data / PREFERENCES_FILE).is_file()
    finally:
        manager.shutdown()

    reopened = BackupCoordinator(data_dir=data, service=service, clock=lambda: 86401)
    try:
        assert reopened.snapshot()["frequency"] == "weekly"
        assert reopened.snapshot()["retention_count"] == 3
        assert reopened.snapshot()["destination"] == str(folder.resolve())
    finally:
        reopened.shutdown()


def test_calendar_due_date_follows_saved_automatic_backup_and_retry(tmp_path):
    now = [86401.0]
    folder = tmp_path / "backups"
    folder.mkdir()
    service = WritingService()
    manager = BackupCoordinator(data_dir=tmp_path / "data", service=service,
                                clock=lambda: now[0])
    try:
        manager.set_schedule("daily", 3, str(folder))
        assert manager.snapshot()["next_backup_at"] == now[0]
        manager._scheduled_tick()
        _wait_for(lambda: bool(service.calls) and not manager.busy)
        assert manager.snapshot()["next_backup_at"] == now[0] + 86400

        manager.create_backup(tmp_path / "manual.owbackup")
        _wait_for(lambda: len(service.calls) == 2 and not manager.busy)
        assert manager.snapshot()["next_backup_at"] == now[0] + 86400
        manager.shutdown()
        manager = BackupCoordinator(data_dir=tmp_path / "data", service=service,
                                    clock=lambda: now[0])
        assert manager.snapshot()["next_backup_at"] == now[0] + 86400

        now[0] += 2 * 86400
        assert manager.snapshot()["next_backup_at"] == now[0]
        manager._retry_after = now[0] + 600
        assert manager.snapshot()["next_backup_at"] == now[0] + 600
        manager.set_schedule("off", 3, str(folder))
        assert manager.snapshot()["next_backup_at"] is None
    finally:
        manager.shutdown()


def test_missing_or_malformed_schedule_destination_does_not_run(tmp_path):
    data = tmp_path / "data"
    folder = tmp_path / "backups"
    folder.mkdir()
    service = WritingService()
    manager = BackupCoordinator(data_dir=data, service=service, clock=lambda: 86401)
    try:
        manager.set_schedule("daily", 2, str(folder))
        folder.rmdir()
        manager._scheduled_tick()
        assert service.calls == []
        assert "destination folder" in manager.snapshot()["last_error"]
    finally:
        manager.shutdown()

    preferences = json.loads((data / PREFERENCES_FILE).read_text(encoding="utf-8"))
    preferences["destination"] = "relative-folder"
    (data / PREFERENCES_FILE).write_text(json.dumps(preferences), encoding="utf-8")
    reopened = BackupCoordinator(data_dir=data, service=service, clock=lambda: 86401)
    try:
        reopened._scheduled_tick()
        assert service.calls == []
        assert "destination folder" in reopened.snapshot()["last_error"]
    finally:
        reopened.shutdown()


def test_automatic_retention_removes_only_registered_archives(tmp_path):
    data = tmp_path / "data"
    folder = tmp_path / "backups"
    folder.mkdir()
    manual = folder / "my-manual.owbackup"
    manual.write_bytes(b"manual")
    unregistered_auto = folder / "OpenWhisper-auto-20261001T000000Z-aaaaaaaaaaaa.owbackup"
    unregistered_auto.write_bytes(b"unregistered")
    unrelated = folder / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")
    now = [86401.0]
    service = WritingService()
    manager = BackupCoordinator(data_dir=data, service=service,
                                clock=lambda: now[0])
    try:
        manager.set_schedule("daily", 2, str(folder))
        created = []
        for _ in range(3):
            before = len(service.calls)
            manager._scheduled_tick()
            _wait_for(lambda: len(service.calls) > before and not manager.busy)
            created.append(Path(service.calls[-1][1]))
            now[0] += 86401
        assert not created[0].exists()
        assert all(path.exists() for path in created[1:])
        assert manual.exists()
        assert unregistered_auto.exists()
        assert unrelated.exists()
        saved = json.loads((data / PREFERENCES_FILE).read_text(encoding="utf-8"))
        assert saved["scheduled_archives"] == [str(path) for path in created[1:]]
    finally:
        manager.shutdown()


def test_inspection_does_not_reserve_but_restore_does(tmp_path):
    service = WritingService()
    source = tmp_path / "backup.owbackup"
    source.write_bytes(b"archive")
    events = []
    manager = BackupCoordinator(data_dir=tmp_path / "data", service=service,
                                acquire=lambda: events.append("acquire"),
                                release=lambda: events.append("release"))
    inspected = []
    staged = []
    manager.inspection_finished.connect(lambda info, error: inspected.append((info, error)))
    manager.restore_staged.connect(lambda path, error: staged.append((path, error)))
    try:
        manager.inspect_backup(source)
        _wait_for(lambda: bool(inspected))
        assert inspected[0][1] == ""
        assert events == []
        manager.prepare_restore(source)
        _wait_for(lambda: bool(staged))
        assert staged == [(str(source), "")]
        assert events == ["acquire", "release"]
    finally:
        manager.shutdown()


def test_snapshot_exposes_previous_data_location_when_available(tmp_path):
    previous = tmp_path / "data" / ".openwhisper-restore" / "previous-123"

    class RecoveryService(WritingService):
        def previous_data_dir(self, data_dir):
            assert Path(data_dir) == (tmp_path / "data").resolve()
            return previous

    service = RecoveryService()
    manager = BackupCoordinator(data_dir=tmp_path / "data", service=service)
    try:
        assert manager.snapshot()["previous_data_dir"] == str(previous)
        service.previous_data_dir = lambda _data_dir: (_ for _ in ()).throw(ValueError("bad metadata"))
        assert manager.snapshot()["previous_data_dir"] == ""
    finally:
        manager.shutdown()
