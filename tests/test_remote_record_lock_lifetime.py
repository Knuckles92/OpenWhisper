"""Upload locks live as long as callers need them, not as long as the host."""
from concurrent.futures import ThreadPoolExecutor
import gc
import hashlib
import threading

from services.remote_records.host_store import HostRecordStore


def test_completed_records_do_not_retain_upload_locks(tmp_path):
    store = HostRecordStore(str(tmp_path), {})
    for index in range(100):
        record_id = f"record-{index}"
        store.begin("device", "dictation", record_id, {"files": 1, "bytes": 0})
        store.abort("device", "dictation", record_id)
    gc.collect()
    assert len(store._locks) == 0


def test_waiting_callers_keep_the_same_lock_alive(tmp_path):
    store = HostRecordStore(str(tmp_path), {})
    first = store._lock("device", "dictation", "record")
    first.acquire()
    acquired = threading.Event()
    waiting = threading.Event()
    release = threading.Event()
    identities = []

    def wait_for_lock():
        lock = store._lock("device", "dictation", "record")
        identities.append(id(lock))
        waiting.set()
        with lock:
            acquired.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(wait_for_lock)
        assert waiting.wait(3)
        assert not acquired.is_set()
        assert identities == [id(first)]
        first.release()
        del first
        assert acquired.wait(3)
        gc.collect()
        assert id(store._lock("device", "dictation", "record")) == identities[0]
        assert len(store._locks) == 1
        release.set()
        future.result(timeout=3)
    gc.collect()
    assert len(store._locks) == 0


def test_concurrent_duplicate_chunks_are_not_appended_twice(tmp_path, monkeypatch):
    from services.remote_records import host_store

    store = HostRecordStore(str(tmp_path), {})
    payload = b"record bytes" * 100
    header = {"name": "audio.wav", "size": len(payload),
              "sha256": hashlib.sha256(payload).hexdigest(), "offset": 0}
    store.begin("device", "dictation", "record", {"files": 1, "bytes": len(payload)})
    entered = threading.Event()
    second_waiting = threading.Event()
    release = threading.Event()
    append = host_store._append_chunk
    calls = []
    original_lock = store._lock

    def observed_lock(*key):
        lock = original_lock(*key)
        if entered.is_set():
            second_waiting.set()
        return lock

    def blocked_append(*args):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return append(*args)

    monkeypatch.setattr(store, "_lock", observed_lock)
    monkeypatch.setattr(host_store, "_append_chunk", blocked_append)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.put, "device", "dictation", "record", header, payload)
        assert entered.wait(3)
        second = pool.submit(store.put, "device", "dictation", "record", header, payload)
        assert second_waiting.wait(3)
        assert not second.done()
        release.set()
        assert first.result(timeout=3) == {"received": len(payload), "done": True}
        assert second.result(timeout=3) == {"received": len(payload), "done": True}
    assert len(calls) == 1
    assert (tmp_path / "staging/device/dictation-record/files/audio.wav").read_bytes() == payload
    gc.collect()
    assert len(store._locks) == 0
