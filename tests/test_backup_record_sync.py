"""Backup must hold local audio stable while the sync worker can move it."""

import threading

from services.remote_records.sync import RecordSync


def test_backup_pauses_upload_and_bring_back_and_resumes(monkeypatch):
    sync = RecordSync(kinds={})
    entered = threading.Event()
    upload = threading.Event()
    restored = threading.Event()
    monkeypatch.setattr(sync, "_run_once", lambda **_: upload.set())
    monkeypatch.setattr(sync, "_bring_back", lambda _: restored.set())

    def work():
        entered.set()
        sync.run_once()
        sync.bring_back()

    with sync.paused_for_backup():
        worker = threading.Thread(target=work)
        worker.start()
        assert entered.wait(1)
        assert not upload.wait(0.1)
        assert not restored.is_set()
    worker.join(2)
    assert not worker.is_alive()
    assert upload.is_set() and restored.is_set()


def test_failed_backup_releases_sync_lock():
    import pytest

    sync = RecordSync(kinds={})
    with pytest.raises(ValueError):
        with sync.paused_for_backup():
            raise ValueError("disk full")
    acquired = []

    def work():
        with sync.paused_for_backup():
            acquired.append(True)

    worker = threading.Thread(target=work)
    worker.start()
    worker.join(2)
    assert acquired == [True]
