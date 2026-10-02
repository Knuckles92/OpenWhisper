"""Registry concurrency with disposable storage and no network connections."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from services.remote_asr import settings as remote_settings, tls
from services.remote_asr.host import DeviceRegistry
from services.remote_asr.service import RemoteEngineService


@pytest.fixture
def service(monkeypatch):
    devices = []

    def save(entries):
        devices[:] = [dict(entry) for entry in entries]

    monkeypatch.setattr(remote_settings, "load_host_devices", lambda: list(devices))
    monkeypatch.setattr(remote_settings, "save_host_devices", save)
    monkeypatch.setattr(
        tls, "ensure_host_identity", lambda _path: SimpleNamespace(fingerprint="fixture")
    )
    service = RemoteEngineService(lambda: None, identity_dir="unused")
    monkeypatch.setattr(service, "tailscale_status", lambda: None)
    yield service
    service.shutdown()


def test_history_and_host_share_registry_before_and_after_host_creation(service):
    registry = service.history.registry
    device, _ = registry.add("Fixture laptop")
    assert service.host_state()["devices"] == registry.list()
    host = service._ensure_host()
    assert host.registry is registry
    assert host.history is service.history
    assert service.host_state()["devices"] == [device]


@pytest.mark.parametrize("create_host", [False, True])
def test_service_removal_uses_history_registry(service, monkeypatch, create_host):
    device, _ = service.history.registry.add("Fixture laptop")
    if create_host:
        service._ensure_host()
    calls = []
    remove = service.history.registry.remove

    def tracked_remove(device_id):
        calls.append(device_id)
        return remove(device_id)

    monkeypatch.setattr(service.history.registry, "remove", tracked_remove)
    assert service.remove_device(device["id"])
    assert calls == [device["id"]]
    assert service.history.registry.list() == []


@pytest.mark.parametrize("mutation", ["add", "remove", "remove_without_host"])
def test_last_seen_write_serializes_with_device_changes(service, monkeypatch, mutation):
    registry = service.history.registry
    device, token = registry.add("Fixture laptop")
    host = None if mutation == "remove_without_host" else service._ensure_host()
    snapshot_ready = threading.Event()
    release_snapshot = threading.Event()
    mutation_attempted = threading.Event()
    contended = []

    class ObservedLock:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            if threading.current_thread().name.startswith("registry-mutation"):
                acquired = self.lock.acquire(blocking=False)
                contended.append(not acquired)
                mutation_attempted.set()
                if acquired:
                    return self
            self.lock.acquire()
            return self

        def __exit__(self, *_exc):
            self.lock.release()

    original_init = DeviceRegistry.__init__

    def observed_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._lock = ObservedLock(self._lock)

    monkeypatch.setattr(DeviceRegistry, "__init__", observed_init)
    for shared in {registry, host.registry if host else registry}:
        shared._lock = ObservedLock(shared._lock)

    save = registry._save

    def paused_save(entries):
        snapshot_ready.set()
        assert release_snapshot.wait(5), "last-seen snapshot was not released"
        save(entries)

    monkeypatch.setattr(registry, "_save", paused_save)

    def change_devices():
        if mutation == "add":
            return host.registry.add("Fixture desktop")[0]
        return service.remove_device(device["id"])

    with ThreadPoolExecutor(max_workers=1) as history_worker, ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="registry-mutation"
    ) as mutation_worker:
        touched = history_worker.submit(registry.authenticate, token)
        changed = None
        try:
            assert snapshot_ready.wait(5), "last-seen snapshot was not captured"
            changed = mutation_worker.submit(change_devices)
            assert mutation_attempted.wait(5), "device change did not attempt the lock"
            # On the regression path, finish the independent writer before
            # releasing the stale snapshot, without relying on a sleep.
            if not contended[0]:
                changed.result(timeout=5)
        finally:
            release_snapshot.set()
        assert touched.result(timeout=5)["id"] == device["id"]
        change = changed.result(timeout=5)

    saved_ids = {entry["id"] for entry in registry.list()}
    if mutation == "add":
        assert saved_ids == {device["id"], change["id"]}
    else:
        assert change is True
        assert saved_ids == set()
    assert contended == [True]
