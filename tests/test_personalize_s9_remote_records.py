"""History entries' newer fields on the paired host, and edits sent again.

A real SpeechHost and HostRecordStore over TLS on 127.0.0.1, as in
tests/test_remote_records.py, whose fixtures these tests share.
"""
from __future__ import annotations

import importlib
import os

import pytest

from services.remote_asr import settings as remote_settings
from services.remote_asr import tailscale
from services.remote_asr.client import RemoteConnection
from services.remote_asr.settings import ClientPairing
from services.remote_records import sync as sync_module
from services.remote_records.kinds import ENTRY_EXT_FIELDS, DictationRecords, MeetingRecords
from services.remote_records.sync import RecordSync
from tests import test_remote_records as remote_tests

EXT = {
    "entry_kind": "dictation",
    "cleanup_level": "medium",
    "cleaned_text": "Hello from the laptop.",
    "language": "en",
}


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: tailscale.TailscaleStatus("not_installed"))
    monkeypatch.setattr(tailscale, "whois", lambda address, timeout=4.0: None)


@pytest.fixture
def host(tmp_path):
    host = remote_tests.Host(tmp_path / "host")
    yield host
    host.close()


@pytest.fixture
def client(host, tmp_path, monkeypatch):
    """This computer, paired with ``host``, as tests/test_remote_records.py sets it up."""
    result = host.pair()
    pairing = ClientPairing("127.0.0.1", host.server.port, result.fingerprint, "devbox",
                            device_id=result.device_id)
    monkeypatch.setattr(remote_settings, "load_client_pairing", lambda settings=None: pairing)
    monkeypatch.setattr(remote_settings, "load_client_token", lambda: result.token)
    sync = RecordSync(
        {"dictation": DictationRecords(), "meeting": MeetingRecords(str(tmp_path / "meetings"))},
        connect=lambda: RemoteConnection("127.0.0.1", host.server.port, result.token,
                                         result.fingerprint),
        cache_dir=str(tmp_path / "cache"),
    )
    monkeypatch.setattr(sync_module.record_sync, "_instance", sync)
    sync.device_id = result.device_id
    yield sync
    sync.stop()


def _history():
    return importlib.import_module("services.history_manager")


def _dictate(tmp_path, **fields):
    source = remote_tests._wav(tmp_path / "take.wav")
    fields.setdefault("text", "Hello from the laptop.")
    fields.setdefault("raw_text", "hello from the laptop um")
    return _history().history_manager.add_entry(
        model="parakeet (cuda)", source_audio_path=source, audio_duration=0.5,
        file_size=os.path.getsize(source), **fields,
    )


def test_the_newer_fields_travel_beside_the_entry_but_the_app_stays_here(host, client, tmp_path):
    client.set_location("host")
    entry = _dictate(tmp_path, app_id="slack.exe", app_name="Slack", app_category="work",
                     cleanup_level="medium", entry_kind="dictation", language="en")
    bundle = client.kinds["dictation"].bundle(None, entry.id)
    assert bundle.record["entry_ext"] == EXT
    assert set(bundle.record["entry"]).isdisjoint(ENTRY_EXT_FIELDS)
    assert "app_name" not in str(bundle.manifest_bytes())

    client.run_once()

    stored = host.db.get_history_entry_by_id(entry.id)
    assert {name: getattr(stored, name) for name in ENTRY_EXT_FIELDS} == EXT
    assert stored.app_name is None and stored.app_id is None
    listed = client.list_remote("dictation")
    assert {name: listed[0][name] for name in ENTRY_EXT_FIELDS} == EXT
    assert "app_name" not in listed[0]

    client.set_location("local")
    assert client.bring_back() == 1
    back = _history().history_manager.get_entry_by_id(entry.id)
    assert {name: getattr(back, name) for name in ENTRY_EXT_FIELDS} == EXT


def test_an_entry_from_an_older_computer_still_imports(host, client, tmp_path, monkeypatch):
    client.set_location("host")
    entry = _dictate(tmp_path, entry_kind="dictation")
    real_bundle = DictationRecords.bundle

    def older_bundle(self, origin, record_id, **kwargs):
        bundle = real_bundle(self, origin, record_id, **kwargs)
        bundle.record.pop("entry_ext")
        return bundle

    monkeypatch.setattr(DictationRecords, "bundle", older_bundle)
    client.run_once()

    stored = host.db.get_history_entry_by_id(entry.id)
    assert stored.text == "Hello from the laptop."
    assert all(getattr(stored, name) is None for name in ENTRY_EXT_FIELDS)


def test_a_newer_computers_extra_fields_are_left_out_not_refused(host, client, tmp_path, monkeypatch):
    client.set_location("host")
    entry = _dictate(tmp_path)
    real_bundle = DictationRecords.bundle

    def newer_bundle(self, origin, record_id, **kwargs):
        bundle = real_bundle(self, origin, record_id, **kwargs)
        bundle.record["entry"]["mood"] = "cheerful"
        bundle.record["entry_ext"].update(language=7, cleanup_level="high", sparkle=True)
        return bundle

    monkeypatch.setattr(DictationRecords, "bundle", newer_bundle)
    client.run_once()

    stored = host.db.get_history_entry_by_id(entry.id)
    assert stored is not None
    assert stored.cleanup_level == "high"
    assert stored.language is None
    assert not hasattr(stored, "mood") and not hasattr(stored, "sparkle")


def test_known_entry_fields_are_still_checked(tmp_path):
    from services.database import DatabaseManager

    database = DatabaseManager(db_path=str(tmp_path / "host.db"))
    try:
        records = DictationRecords(str(tmp_path / "recordings"), database=database)
        record = {"kind": "dictation", "format": 1, "audio": None, "entry": {
            "id": "e1", "text": 42, "timestamp": "2026-10-06T10:00:00+00:00", "model": "base",
        }}
        with pytest.raises(ValueError, match="text"):
            records.import_record("e1", str(tmp_path), record, {"id": "device-1", "name": "laptop"})
        record["entry"]["text"] = "fine"
        record["entry_ext"] = ["not", "an", "object"]
        records.import_record("e1", str(tmp_path), record, {"id": "device-1", "name": "laptop"})
        assert database.get_history_entry_by_id("e1").text == "fine"
    finally:
        database.close()


def test_choosing_the_original_sends_the_host_copy_again(host, client, tmp_path):
    history = _history()
    client.set_location("both")
    entry = _dictate(tmp_path)
    client.run_once()
    assert host.db.get_history_entry_by_id(entry.id).text == "Hello from the laptop."
    before = client.kinds["dictation"].digest(entry.id)
    assert client.kinds["dictation"].digest(entry.id) == before

    history.history_manager.use_version(entry.id, history.ORIGINAL_VERSION)

    assert client.kinds["dictation"].digest(entry.id) != before
    assert client._row("dictation", entry.id).state == "pending"
    client.run_once()
    stored = host.db.get_history_entry_by_id(entry.id)
    assert (stored.text, stored.raw_text, stored.cleaned_text) == (
        "hello from the laptop um", "hello from the laptop um", "Hello from the laptop.",
    )
    assert client._row("dictation", entry.id).state == "done"
    assert history.history_manager.get_entry_by_id(entry.id) is not None


def test_an_edit_to_a_record_never_sent_queues_nothing(host, client, tmp_path):
    history = _history()
    entry = _dictate(tmp_path)
    history.history_manager.use_version(entry.id, history.ORIGINAL_VERSION)
    assert client._rows() == []


def test_an_edited_viewing_copy_goes_back_to_the_host(host, client, tmp_path):
    history = _history()
    client.set_location("host")
    entry = _dictate(tmp_path)
    client.run_once()
    assert history.history_manager.get_entry_by_id(entry.id) is None

    client.check_out("dictation", entry.id)
    history.history_manager.use_version(entry.id, history.ORIGINAL_VERSION)
    client.run_once(startup=True)

    assert host.db.get_history_entry_by_id(entry.id).text == "hello from the laptop um"
    assert history.history_manager.get_entry_by_id(entry.id) is None
