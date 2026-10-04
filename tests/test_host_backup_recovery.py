"""Host restore ownership and backup pauses over real paired TLS connections."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from services import backup
from services.remote_asr import settings as remote_settings
from services.remote_asr.client import RemoteConnection, RemoteRequestError
from services.remote_asr.host import DeviceRegistry
from services.remote_asr.service import RemoteEngineService
from services.remote_asr.settings import ClientPairing
from services.remote_records.gate import RecordsBusy
from tests import test_remote_records as record_fixtures
from tests.test_remote_records import Host, _dictate, _meeting

host = record_fixtures.host
client = record_fixtures.client


def _wire_service(host):
    service = RemoteEngineService(lambda: None)
    service._records = host.store
    service._registry = host.server.registry
    host.server._records = service.records_request
    host.server._records_summary = service.records_summary
    return service


def test_restored_host_recovery_requires_local_assignment_and_preserves_both_owners(
    host, client, tmp_path, monkeypatch,
):
    from services.history_manager import history_manager
    from services.database import db

    client.set_location("host")
    entry = _dictate(tmp_path, "Before host restore")
    _meeting(client)
    client.record_saved("meeting", "m_0123456789ab")
    client.run_once()
    original_owner = client.device_id
    host.server.stop()
    archive = tmp_path / "host.owbackup"
    backup.create_backup(archive, data_dir=host.root)
    restored_root = tmp_path / "restored-host"
    backup.prepare_restore(archive, data_dir=restored_root)
    backup.apply_pending_restore(data_dir=restored_root)
    restored = Host(restored_root)
    service = _wire_service(restored)
    try:
        paired = restored.pair("laptop")
        other = restored.pair("laptop")
        pairing = ClientPairing("127.0.0.1", restored.server.port, paired.fingerprint,
                                "devbox", device_id=paired.device_id)
        monkeypatch.setattr(remote_settings, "load_client_pairing", lambda settings=None: pairing)
        monkeypatch.setattr(remote_settings, "load_client_token", lambda: paired.token)
        client._connect_fn = lambda: RemoteConnection(
            "127.0.0.1", restored.server.port, paired.token, paired.fingerprint,
        )
        assert paired.device_id != original_owner
        assert client.list_remote("dictation") == []
        new_entry = _dictate(tmp_path, "After re-pairing")
        client.run_once()
        assert [item["id"] for item in client.list_remote("dictation")] == [new_entry.id]
        assert service.recoverable_record_owners() == [{
            "id": original_owner, "name": "laptop", "counts": {"dictation": 1, "meeting": 1},
        }]

        stranger = RemoteConnection("127.0.0.1", restored.server.port, other.token, other.fingerprint)
        try:
            stranger.connect()
            with pytest.raises(RemoteRequestError):
                stranger.request("records_open", kind="dictation", record_id=entry.id,
                                 record_owner_ids=[original_owner])
            assert stranger.request("records_list", kind="dictation",
                                    record_owner_ids=[original_owner])["records"] == []
            with pytest.raises(RemoteRequestError):
                stranger.request("records_recover", kind="dictation", record_id=entry.id,
                                 owner_id=original_owner)

            service.recover_device_records(original_owner, paired.device_id)
            assert {item["id"] for item in client.list_remote("dictation")} == {entry.id, new_entry.id}
            assert [item["id"] for item in client.list_remote("meeting")] == ["m_0123456789ab"]
            assert service.records_summary(paired.device_id)["dictation"]["count"] == 2
            assert service.recoverable_record_owners() == []
            with pytest.raises(ValueError, match="no longer available"):
                service.recover_device_records(original_owner, other.device_id)
            assert stranger.request("records_list", kind="dictation")["records"] == []
            with pytest.raises(RemoteRequestError):
                stranger.request("records_open", kind="dictation", record_id=entry.id)
        finally:
            stranger.close()

        # Reloading the registry preserves the host-owner grant, without
        # moving audio or changing either computer's authenticated identity.
        saved = restored.devices.load()
        assert next(device for device in saved if device["id"] == paired.device_id)["record_owner_ids"] == [original_owner]
        registry = DeviceRegistry(restored.devices.load, restored.devices.save)
        assert registry.authenticate(paired.token)["record_owner_ids"] == [original_owner]
        service._registry = restored.server.registry = registry

        # Editing a recovered meeting reuses its original audio and owner.
        repository = client.kinds["meeting"].repository
        original_spool = restored.repository.get_meeting("m_0123456789ab")["spool_dir"]
        client.check_out("meeting", "m_0123456789ab")
        repository.rename_meeting("m_0123456789ab", "Recovered planning, edited")
        client.run_once(startup=True)
        edited = restored.repository.get_meeting("m_0123456789ab")
        assert edited["title"] == "Recovered planning, edited"
        assert edited["origin_device_id"] == original_owner
        assert edited["spool_dir"] == original_spool
        assert repository.get_meeting("m_0123456789ab") is None

        assert client.bring_back() == 3
        assert history_manager.get_entry_by_id(entry.id).text == "Before host restore"
        assert history_manager.get_recording_path(history_manager.get_entry_by_id(entry.id).audio_file)
        assert db.get_history_entry_by_id(new_entry.id) is not None
        assert client.kinds["meeting"].repository.get_meeting("m_0123456789ab")["origin_device_id"] is None
        assert restored.db.get_history_entries() == []
        assert restored.repository.get_meeting("m_0123456789ab") is None
    finally:
        service.shutdown()
        restored.close()


@pytest.mark.parametrize("delete_records", [False, True])
def test_removing_recovery_pairing_keeps_or_deletes_all_granted_records(
    host, client, tmp_path, delete_records,
):
    client.set_location("host")
    entry = _dictate(tmp_path)
    _meeting(client)
    client.record_saved("meeting", "m_0123456789ab")
    client.run_once()
    service = _wire_service(host)
    try:
        paired = host.pair("new laptop")
        assert service.remove_device(client.device_id)
        service.recover_device_records(client.device_id, paired.device_id)
        assert service.remove_device(paired.device_id, delete_records=delete_records)
        if delete_records:
            assert host.db.get_history_entry_by_id(entry.id) is None
            assert host.repository.get_meeting("m_0123456789ab") is None
            assert service.recoverable_record_owners() == []
        else:
            assert host.db.get_history_entry_by_id(entry.id) is not None
            assert host.repository.get_meeting("m_0123456789ab") is not None
            assert service.recoverable_record_owners() == [{
                "id": client.device_id, "name": "laptop", "counts": {"dictation": 1, "meeting": 1},
            }]
    finally:
        service.shutdown()


def test_backup_busy_is_retryable_and_never_exhausts_client_records(host, client, tmp_path):
    from services.history_manager import history_manager

    service = _wire_service(host)
    client.set_location("host")
    entry = _dictate(tmp_path)
    connection = client._open()
    try:
        with service.paused_records_for_backup():
            with pytest.raises(RemoteRequestError) as error:
                connection.request("records_list", kind="dictation")
            assert error.value.retryable and error.value.code == "busy"
            for _ in range(6):
                client.wake(now=True)
                client.run_once()
            row = client._rows()[0]
            assert row.state == "pending" and row.attempts == 0
            assert history_manager.get_entry_by_id(entry.id) is not None
            assert host.db.get_history_entry_by_id(entry.id) is None
        client.wake(now=True)
        client.run_once()
        assert history_manager.get_entry_by_id(entry.id) is None
        assert host.db.get_history_entry_by_id(entry.id) is not None
    finally:
        connection.close()
        service.shutdown()


def test_backup_drains_active_host_mutation_and_releases_gate_after_failure(host, monkeypatch):
    service = _wire_service(host)
    entered, finish, draining, backing_up = (threading.Event() for _ in range(4))
    calls = []

    def mutate(*_args):
        entered.set()
        assert finish.wait(3)
        calls.append("committed")
        return {}

    wait = service._record_gate._condition.wait

    def observed_wait(timeout):
        draining.set()
        return wait(timeout)

    monkeypatch.setattr(host.store, "handle", mutate)
    monkeypatch.setattr(service._record_gate._condition, "wait", observed_wait)

    def backup_work():
        with service.paused_records_for_backup():
            assert calls == ["committed"]
            backing_up.set()
            with pytest.raises(RecordsBusy):
                service.records_request("records_delete", {}, b"", {"id": "peer"})
            raise ValueError("disk full")

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            incoming = workers.submit(service.records_request, "records_commit", {}, b"", {"id": "peer"})
            assert entered.wait(3)
            snapshot = workers.submit(backup_work)
            assert draining.wait(3)
            assert not backing_up.is_set()
            with pytest.raises(RecordsBusy):
                service.records_request("records_put", {}, b"", {"id": "peer"})
            finish.set()
            assert incoming.result(timeout=3) == {}
            with pytest.raises(ValueError, match="disk full"):
                snapshot.result(timeout=3)
        assert service.records_request("records_commit", {}, b"", {"id": "peer"}) == {}
    finally:
        finish.set()
        service.shutdown()


def test_backup_timeout_reopens_gate_without_losing_active_request():
    from services.remote_records.gate import RecordGate

    gate = RecordGate()
    with gate.request():
        with pytest.raises(RecordsBusy, match="still finishing"):
            with gate.paused(timeout=0):
                pytest.fail("A running request cannot be backed up yet")
        with gate.request():
            pass
    with gate.paused(timeout=0):
        with pytest.raises(RecordsBusy):
            with gate.request():
                pass
