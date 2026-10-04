"""Records kept on the paired host: upload, verification, listing, bring back.

A real SpeechHost serves a real HostRecordStore over TLS on 127.0.0.1. The
host has its own database and folders; the client is the test's isolated
app database, as it would be on the laptop.
"""
from __future__ import annotations

import json
import os
import wave
from datetime import datetime, timezone

import numpy as np
import pytest

from services.database import DatabaseManager
from services.remote_asr import protocol, tailscale
from services.remote_asr import settings as remote_settings
from services.remote_asr.client import RemoteConnection, pair_with_host
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.settings import ClientPairing
from services.remote_asr.tls import ensure_host_identity
from services.remote_records import sync as sync_module
from services.remote_records.host_store import HostRecordStore, safe_name
from services.remote_records.kinds import DictationRecords, MeetingRecords
from services.remote_records.sync import RecordSync
from tests.test_remote_engine import FakeEngine, ListStore


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: tailscale.TailscaleStatus("not_installed"))
    monkeypatch.setattr(tailscale, "whois", lambda address, timeout=4.0: None)


def _wav(path, seconds=0.5, rate=16000):
    samples = (0.1 * np.sin(np.arange(int(seconds * rate)) / 8) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return str(path)


class Host:
    """The dev box: its own database, recordings and meetings folders."""

    def __init__(self, root):
        from meeting.persist.repository import SqlMeetingRepository

        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = DatabaseManager(db_path=str(root / "openwhisper.db"))
        self.recordings = root / "recordings"
        self.meetings = root / "meetings"
        self.repository = SqlMeetingRepository(db=self.db)
        self.keeps = True
        self.store = HostRecordStore(str(root / "remote_records"), {
            "dictation": DictationRecords(str(self.recordings), database=self.db),
            "meeting": MeetingRecords(str(self.meetings), repository=self.repository),
        })
        self.devices = ListStore()
        self.server = SpeechHost(
            engine_provider=lambda: FakeEngine(),
            registry=DeviceRegistry(self.devices.load, self.devices.save),
            identity=ensure_host_identity(str(root / "identity")),
            host_name="devbox",
            records_enabled=lambda: self.keeps,
            records=self.store.handle,
            records_summary=self.store.summary,
        )
        self.server.start(port=0, bind="127.0.0.1")

    def pair(self, name="laptop"):
        return pair_with_host("127.0.0.1", self.server.port, self.server.open_pairing(), name)

    def close(self):
        self.server.stop()
        self.db.close()


@pytest.fixture
def host(tmp_path):
    host = Host(tmp_path / "host")
    yield host
    host.close()


@pytest.fixture
def client(host, tmp_path, monkeypatch):
    """This computer, paired with ``host``, its record sync and meeting folder."""
    result = host.pair()
    pairing = ClientPairing("127.0.0.1", host.server.port, result.fingerprint, "devbox",
                            device_id=result.device_id)
    monkeypatch.setattr(remote_settings, "load_client_pairing", lambda settings=None: pairing)
    monkeypatch.setattr(remote_settings, "load_client_token", lambda: result.token)
    meetings = tmp_path / "client_meetings"
    sync = RecordSync(
        {"dictation": DictationRecords(), "meeting": MeetingRecords(str(meetings))},
        connect=lambda: RemoteConnection("127.0.0.1", host.server.port, result.token,
                                         result.fingerprint),
        cache_dir=str(tmp_path / "cache"),
    )
    # The hooks in history_manager and data_lifecycle reach this instance.
    monkeypatch.setattr(sync_module.record_sync, "_instance", sync)
    sync.device_id = result.device_id
    sync.meetings_root = str(meetings)
    yield sync
    sync.stop()


def _dictate(tmp_path, text="hello from the laptop"):
    from services.history_manager import history_manager

    source = _wav(tmp_path / f"take-{abs(hash(text))}.wav")
    return history_manager.add_entry(text=text, model="parakeet (cuda)", source_audio_path=source,
                                     audio_duration=0.5, file_size=os.path.getsize(source))


# ---- the building blocks ----

@pytest.mark.parametrize("name", [
    "../x", "a/../../b", "/etc/passwd", "C:/x", "a\\b", ".hidden", "a//b", "", "a/" * 6 + "b",
])
def test_unsafe_names_are_refused(name):
    with pytest.raises(ValueError):
        safe_name(name)


def test_safe_names_pass():
    assert safe_name("audio/mic_00001.wav") == "audio/mic_00001.wav"
    assert safe_name("record.json") == "record.json"


def test_payload_frames_carry_raw_bytes_and_audio_frames_still_decode():
    header, payload = protocol.unpack_frame(protocol.pack_request({"op": "records_put"}, payload=b"\x01\x02\x03"))
    assert header == {"op": "records_put"} and payload == b"\x01\x02\x03"
    audio = np.array([0.0, 0.5, -0.5], dtype=np.float32)
    _header, decoded = protocol.unpack_request(protocol.pack_request({"op": "transcribe"}, audio))
    assert np.allclose(decoded, audio, atol=1 / 32768)


# ---- dictation ----

def test_ready_advertises_records_and_counts(host, client):
    connection = client._open()
    try:
        assert connection.ready["capabilities"]["records"] is True
        assert connection.ready["records"]["dictation"] == {"count": 0, "bytes": 0}
    finally:
        connection.close()


def test_host_mode_moves_an_entry_with_its_audio(host, client, tmp_path):
    from services.history_manager import history_manager

    client.set_location("host")
    entry = _dictate(tmp_path)
    local_audio = history_manager.get_recording_path(entry.audio_file)
    audio_bytes = open(local_audio, "rb").read()

    client.run_once()

    assert history_manager.get_entry_by_id(entry.id) is None
    assert not os.path.exists(local_audio)
    stored = host.db.get_history_entry_by_id(entry.id)
    assert stored.text == "hello from the laptop"
    assert stored.origin_device_id == client.device_id
    assert stored.origin_device_name == "laptop"
    assert stored.audio_file.startswith(f"devices/{client.device_id}/")
    assert open(os.path.join(host.recordings, *stored.audio_file.split("/")), "rb").read() == audio_bytes
    # The host's own history view lists it; its own-only view doesn't.
    assert [e.id for e in host.db.get_history_entries()] == [entry.id]
    assert host.db.get_history_entries(origin=None) == []

    listed = client.list_remote("dictation")
    assert [item["id"] for item in listed] == [entry.id]
    assert listed[0]["has_audio"] is True and listed[0]["stored_on"] == "devbox"
    assert "origin_device_id" not in listed[0]

    fetched = client.audio_for(entry.id)
    assert open(fetched, "rb").read() == audio_bytes

    assert client.delete_remote("dictation", entry.id) is True
    assert host.db.get_history_entry_by_id(entry.id) is None
    assert client.list_remote("dictation") == []


def test_both_mode_copies_and_a_delete_here_deletes_the_host_copy(host, client, tmp_path):
    from services.history_manager import history_manager

    client.set_location("both")
    entry = _dictate(tmp_path)
    client.run_once()
    assert history_manager.get_entry_by_id(entry.id) is not None
    assert host.db.get_history_entry_by_id(entry.id) is not None
    assert client.copies_on_host("dictation", [entry.id]) == {entry.id}

    history_manager.delete_entry(entry.id, delete_audio_file=True)
    client.run_once()
    assert host.db.get_history_entry_by_id(entry.id) is None
    assert client._rows() == []


def test_local_mode_sends_nothing(host, client, tmp_path):
    entry = _dictate(tmp_path)
    client.run_once()
    assert host.db.get_history_entry_by_id(entry.id) is None
    assert client._rows() == []


def test_a_host_that_doesnt_keep_records_leaves_them_here(host, client, tmp_path):
    from services.history_manager import history_manager

    host.keeps = False
    client.set_location("host")
    entry = _dictate(tmp_path)
    client.run_once()
    assert history_manager.get_entry_by_id(entry.id) is not None
    status = client.status()
    assert status.host_keeps is False and status.pending == 1
    assert "Keep records for paired computers" in status.waiting

    host.keeps = True
    client.wake(now=True)
    client.run_once()
    assert history_manager.get_entry_by_id(entry.id) is None
    assert host.db.get_history_entry_by_id(entry.id) is not None


def test_turning_storage_off_still_lets_a_device_read_and_delete_its_records(host, client, tmp_path):
    client.set_location("host")
    entry = _dictate(tmp_path)
    client.run_once()
    host.keeps = False
    assert [item["id"] for item in client.list_remote("dictation")] == [entry.id]
    assert client.delete_remote("dictation", entry.id) is True


def test_devices_only_see_their_own_records(host, client, tmp_path):
    client.set_location("host")
    entry = _dictate(tmp_path)
    client.run_once()

    other = host.pair("intruder")
    connection = RemoteConnection("127.0.0.1", host.server.port, other.token, other.fingerprint)
    connection.connect()
    try:
        assert connection.request("records_list", kind="dictation")["records"] == []
        with pytest.raises(RuntimeError, match="isn't here"):
            connection.request("records_open", kind="dictation", record_id=entry.id)
        assert connection.request("records_delete", kind="dictation", record_id=entry.id) == {"deleted": False}
    finally:
        connection.close()
    assert host.db.get_history_entry_by_id(entry.id) is not None


def test_a_damaged_file_is_refused_and_sent_again(host, client, tmp_path):
    from services.history_manager import history_manager

    client.set_location("host")
    entry = _dictate(tmp_path)
    connection = client._open()
    try:
        bundle = client.kinds["dictation"].bundle(None, entry.id)
        name, path = next(iter(bundle.files.items()))
        data = open(path, "rb").read()
        connection.request("records_begin", kind="dictation", record_id=entry.id, files=2,
                           bytes=len(data) + len(bundle.manifest_bytes()))
        with pytest.raises(RuntimeError, match="damaged"):
            connection.request("records_put", kind="dictation", record_id=entry.id, name=name,
                               size=len(data), sha256="0" * 64, offset=0, payload=data)
        with pytest.raises(RuntimeError, match="isn't complete"):
            connection.request("records_commit", kind="dictation", record_id=entry.id)
    finally:
        connection.close()
    client.run_once()
    assert history_manager.get_entry_by_id(entry.id) is None
    assert host.db.get_history_entry_by_id(entry.id) is not None


def test_an_interrupted_upload_resumes_where_it_stopped(host, client, tmp_path, monkeypatch):
    client.set_location("host")
    source = _wav(tmp_path / "long.wav", seconds=70)  # ~2.2 MB: three chunks
    from services.history_manager import history_manager

    entry = history_manager.add_entry(text="long one", model="parakeet", source_audio_path=source)
    real_request = RemoteConnection.request
    puts = []

    def flaky(self, op, **fields):
        if op == "records_put":
            puts.append(fields["offset"])
            if len(puts) == 2:
                self.close()
                raise RuntimeError("Transcription canceled")
        return real_request(self, op, **fields)

    monkeypatch.setattr(RemoteConnection, "request", flaky)
    client.run_once()
    assert history_manager.get_entry_by_id(entry.id) is not None
    monkeypatch.setattr(RemoteConnection, "request", real_request)
    received = []
    monkeypatch.setattr(RemoteConnection, "request", lambda self, op, **fields: (
        received.append((op, fields.get("offset"))), real_request(self, op, **fields))[1])
    client.wake(now=True)
    client.run_once()
    assert host.db.get_history_entry_by_id(entry.id) is not None
    first_put = next(offset for op, offset in received if op == "records_put")
    assert first_put == protocol.RECORD_CHUNK_BYTES


def test_clearing_history_here_clears_it_on_the_host(host, client, tmp_path):
    from services.history_manager import history_manager

    client.set_location("host")
    _dictate(tmp_path, "one")
    _dictate(tmp_path, "two")
    client.run_once()
    assert len(host.db.get_history_entries()) == 2
    history_manager.clear_history()
    client.run_once()
    assert host.db.get_history_entries() == []


def test_the_hosts_clear_keeps_paired_computers_entries(host, client, tmp_path):
    client.set_location("host")
    entry = _dictate(tmp_path)
    client.run_once()
    host.db.clear_history()
    assert host.db.get_history_entry_by_id(entry.id) is not None


def test_bring_back_moves_everything_home(host, client, tmp_path):
    from services.history_manager import history_manager

    client.set_location("host")
    entry = _dictate(tmp_path)
    client.run_once()
    client.set_location("local")
    assert client.bring_back() == 1
    back = history_manager.get_entry_by_id(entry.id)
    assert back is not None and back.origin_device_id is None
    assert history_manager.get_recording_path(back.audio_file)
    assert host.db.get_history_entry_by_id(entry.id) is None


def test_host_retention_follows_the_devices_limits(host, client, tmp_path, monkeypatch):

    client.set_location("host")
    monkeypatch.setattr(client, "_retention", lambda: {"max_recordings": 1, "max_bytes": None})
    first = _dictate(tmp_path, "first")
    client.run_once()
    # Recordings are named by the second they were saved.
    import time
    time.sleep(1.1)
    second = _dictate(tmp_path, "second")
    client.run_once()
    assert host.db.get_history_entry_by_id(first.id).audio_file is None
    assert host.db.get_history_entry_by_id(second.id).audio_file


# ---- meetings ----

def _meeting(sync, meeting_id="m_0123456789ab", status="ended"):
    """A finished two-chunk meeting in this computer's database and folder."""
    from meeting.persist.repository import SqlMeetingRepository
    from services.database import db
    from services.models import MeetingParticipant, MeetingSegment

    repository = SqlMeetingRepository()
    spool = os.path.join(sync.meetings_root, meeting_id[:12])
    os.makedirs(spool, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    state = {"meeting_id": meeting_id, "title": "Planning", "topic": {"current": "Roadmap"},
             "finalization": {"status": "completed"}}
    repository.create_meeting(
        id=meeting_id, title="Planning", status=status, started_at=now, ended_at=now,
        host_token="host-secret", guest_token="guest-secret", spool_dir=spool,
        state_json=json.dumps(state), asr_model="parakeet-v3",
    )
    chunk_ids = []
    for seq, channel in enumerate(("mic", "loopback")):
        path = _wav(os.path.join(spool, f"{channel}_{seq:05d}.wav"), seconds=0.25)
        chunk_ids.append(repository.register_chunk(
            meeting_id=meeting_id, channel=channel, seq=seq, file_path=path, start_s=0.0,
            duration_s=0.25, sample_rate=16000, asr_status="done",
        ))
    _wav(os.path.join(spool, "mic_session.wav"), seconds=0.25)
    with open(os.path.join(spool, "mic_session.json"), "w") as handle:
        json.dump({"sample_rate": 16000}, handle)
    # Leftovers that must never travel.
    open(os.path.join(spool, "mic_session.pcm"), "wb").write(b"\x00" * 64)
    open(os.path.join(spool, "playback.wav"), "wb").write(b"RIFF")
    with db.get_session() as session:
        session.add(MeetingParticipant(id="p_me", meeting_id=meeting_id, display_name="Me",
                                       kind="me", created_at=now, updated_at=now))
        session.add(MeetingSegment(id="sg_one", meeting_id=meeting_id, chunk_id=chunk_ids[1],
                                   channel="loopback", start_s=0.0, end_s=0.25, text="Ship it Friday",
                                   speaker_participant_id="p_me", embedding=b"\x01\x02",
                                   created_at=now))
    return repository


def test_a_finished_meeting_moves_with_its_audio_and_rows(host, client):
    client.set_location("host")
    repository = _meeting(client)
    client.record_saved("meeting", "m_0123456789ab")
    client.run_once()

    assert repository.get_meeting("m_0123456789ab") is None
    stored = host.repository.get_meeting("m_0123456789ab")
    assert stored["origin_device_id"] == client.device_id
    assert stored["host_token"] != "host-secret" and stored["guest_token"] != "guest-secret"
    spool = stored["spool_dir"]
    assert os.path.dirname(spool) == str(host.meetings)
    assert sorted(os.listdir(spool)) == [
        "loopback_00001.wav", "mic_00000.wav", "mic_session.json", "mic_session.wav",
    ]
    chunks = host.repository.get_audio_chunks("m_0123456789ab")
    assert {os.path.dirname(chunk["file_path"]) for chunk in chunks} == {spool}
    segments = host.repository.get_segments("m_0123456789ab")
    loopback = next(chunk for chunk in chunks if chunk["channel"] == "loopback")
    assert segments[0]["chunk_id"] == loopback["id"]
    assert segments[0]["text"] == "Ship it Friday"
    # Search sees the imported transcript through the FTS triggers.
    assert host.repository.list_past_meeting_summaries(query="Friday")[0]["id"] == "m_0123456789ab"

    listed = client.list_remote("meeting")
    assert listed[0]["id"] == "m_0123456789ab"
    assert "host_token" not in listed[0] and "spool_dir" not in listed[0]
    assert json.loads(listed[0]["state_json"])["title"] == "Planning"


def test_an_unfinished_meeting_waits(host, client):
    client.set_location("host")
    repository = _meeting(client, status="active")
    client.record_saved("meeting", "m_0123456789ab")
    client.run_once()
    assert repository.get_meeting("m_0123456789ab") is not None
    assert host.repository.get_meeting("m_0123456789ab") is None
    assert "hasn't finished" in client.status().waiting


def test_a_meeting_open_here_stays_until_it_closes(host, client):
    client.set_location("host")
    repository = _meeting(client)
    meetings = client.kinds["meeting"]
    meetings.in_use_check = lambda meeting_id: True
    client.record_saved("meeting", "m_0123456789ab")
    client.run_once()
    assert host.repository.get_meeting("m_0123456789ab") is not None
    assert repository.get_meeting("m_0123456789ab") is not None
    repository.rename_meeting("m_0123456789ab", "Planning, edited")
    meetings.in_use_check = lambda meeting_id: False
    client.run_once(startup=True)
    assert repository.get_meeting("m_0123456789ab") is None
    assert host.repository.get_meeting("m_0123456789ab")["title"] == "Planning, edited"


def test_an_edited_copy_is_sent_again_without_its_audio(host, client, monkeypatch):
    client.set_location("both")
    repository = _meeting(client)
    client.record_saved("meeting", "m_0123456789ab")
    client.run_once()
    repository.rename_meeting("m_0123456789ab", "Planning v2")

    names = []
    real_request = RemoteConnection.request

    def spy(self, op, **fields):
        if op == "records_put":
            names.append(fields["name"])
        return real_request(self, op, **fields)

    monkeypatch.setattr(RemoteConnection, "request", spy)
    client.run_once(startup=True)
    assert names == ["record.json"]
    stored = host.repository.get_meeting("m_0123456789ab")
    assert stored["title"] == "Planning v2"
    assert len(os.listdir(stored["spool_dir"])) == 4


def test_checking_out_a_host_meeting_and_bringing_meetings_back(host, client):
    client.set_location("host")
    repository = _meeting(client)
    client.record_saved("meeting", "m_0123456789ab")
    client.run_once()
    assert repository.get_meeting("m_0123456789ab") is None

    client.check_out("meeting", "m_0123456789ab")
    local = repository.get_meeting("m_0123456789ab")
    assert local is not None and local["origin_device_id"] is None
    assert os.path.dirname(local["spool_dir"]) == client.meetings_root
    assert len(repository.get_audio_chunks("m_0123456789ab")) == 2
    # The next start returns the unchanged viewing copy.
    client.run_once(startup=True)
    assert repository.get_meeting("m_0123456789ab") is None
    assert host.repository.get_meeting("m_0123456789ab") is not None

    client.set_location("local")
    assert client.bring_back() == 1
    assert repository.get_meeting("m_0123456789ab") is not None
    assert host.repository.get_meeting("m_0123456789ab") is None


def test_a_meeting_record_with_traversal_is_refused(host, client):
    connection = client._open()
    try:
        record = {"kind": "meeting", "format": 1, "session": {"id": "m_0123456789ab"},
                  "files": {"../../evil.wav": 4}}
        body = json.dumps(record).encode()
        import hashlib
        connection.request("records_begin", kind="meeting", record_id="m_0123456789ab", files=1,
                           bytes=len(body))
        connection.request("records_put", kind="meeting", record_id="m_0123456789ab",
                           name="record.json", size=len(body),
                           sha256=hashlib.sha256(body).hexdigest(), offset=0, payload=body)
        with pytest.raises(RuntimeError):
            connection.request("records_commit", kind="meeting", record_id="m_0123456789ab")
        with pytest.raises(RuntimeError, match="Invalid file name"):
            connection.request("records_put", kind="meeting", record_id="m_0123456789ab",
                               name="../x.wav", size=1, sha256="0" * 64, offset=0, payload=b"x")
    finally:
        connection.close()
    assert host.repository.get_meeting("m_0123456789ab") is None
    assert not os.path.exists(host.root / "evil.wav")


def test_removing_a_device_can_delete_what_it_stored(host, client, tmp_path):
    client.set_location("host")
    _meeting(client)
    client.record_saved("meeting", "m_0123456789ab")
    entry = _dictate(tmp_path)
    client.run_once()
    assert host.store.summary(client.device_id)["meeting"]["count"] == 1
    assert host.store.delete_device(client.device_id) == 2
    assert host.repository.get_meeting("m_0123456789ab") is None
    assert host.db.get_history_entry_by_id(entry.id) is None
