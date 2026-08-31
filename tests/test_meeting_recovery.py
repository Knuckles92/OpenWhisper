"""Focused tests for Meeting Mode crash-recovery scan, finalize, and discard."""
import json
import os
import wave
from datetime import datetime, timedelta

import numpy as np

from meeting.interfaces import SpooledChunk
from meeting.recovery import (
    STALE_HEARTBEAT_S,
    _mark_ended,
    _pid_alive,
    finalize_meeting,
    find_recoverable_meetings,
    is_session_dead,
    reconcile_meeting_audio,
)
from meeting.state.schema import MeetingState


class FakeRepository:
    def __init__(self):
        self.updates = []
        self.interrupted = []
        self.pending = []
        self.unfinished = 0
        self.reset_calls = []
        self.commits = []
        self.fail_list = False
        self.fail_pending = False

    def update_meeting(self, meeting_id, **fields):
        self.updates.append((meeting_id, fields))

    def find_interrupted_meetings(self):
        if self.fail_list:
            raise RuntimeError("db down")
        return list(self.interrupted)

    def reset_unfinished_chunks(self, meeting_id):
        if self.fail_pending:
            raise RuntimeError("chunks unavailable")
        self.reset_calls.append(meeting_id)
        return len(self.pending)

    def get_pending_chunks(self, meeting_id):
        if self.fail_pending:
            raise RuntimeError("chunks unavailable")
        return list(self.pending)

    def count_unfinished_chunks(self, meeting_id):
        return self.unfinished

    def commit_chunk_transcription(self, meeting_id, chunk_id, segments):
        self.commits.append((meeting_id, chunk_id, list(segments)))


def _iso_age(seconds: float) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).isoformat()


def _pending_chunk(meeting_id="m_crash", chunk_id=7):
    return {
        "id": chunk_id,
        "meeting_id": meeting_id,
        "channel": "mic",
        "seq": 0,
        "file_path": "/tmp/chunk.wav",
        "start_s": 1.5,
        "duration_s": 4.0,
        "sample_rate": 16000,
    }


class FakeAsrEngine:
    """Stand-in for MeetingAsrEngine used by headless finalize."""

    instances = []

    def __init__(self, model_name, meeting_id, repository, language=None):
        self.model_name = model_name
        self.meeting_id = meeting_id
        self.repository = repository
        self.language = language
        self.is_available = True
        self.started = False
        self.enqueued = []
        self.stopped = False
        self.drain_ok = True
        self._cb = None
        FakeAsrEngine.instances.append(self)

    def start(self, cb):
        self._cb = cb
        self.started = True

    def enqueue(self, chunk):
        self.enqueued.append(chunk)
        if self._cb is not None:
            self._cb(chunk, [])

    def drain(self, timeout):
        return self.drain_ok

    def stop(self):
        self.stopped = True


def test_mark_ended_normalizes_interrupted_finalization():
    """Headless ASR finalize must leave a durable finalization outcome."""
    state = MeetingState(
        meeting_id="m_crash",
        title="Crash mid-finalize",
        status="active",
        cloud_enabled=True,
    )
    payload = state.to_dict()
    payload["finalization"] = {
        "status": "running",
        "message": "Preparing final cloud insights…",
    }
    meeting = {
        "id": "m_crash",
        "cloud_enabled": True,
        "state_json": json.dumps(payload),
    }
    repo = FakeRepository()

    _mark_ended(repo, meeting)

    assert len(repo.updates) == 1
    meeting_id, fields = repo.updates[0]
    assert meeting_id == "m_crash"
    assert fields["status"] == "ended"
    assert "ended_at" in fields
    patched = json.loads(fields["state_json"])
    assert patched["status"] == "ended"
    assert patched["finalization"]["status"] == "failed"
    assert "interrupted" in patched["finalization"]["message"].lower()


def test_mark_ended_pending_becomes_unavailable():
    state = MeetingState(
        meeting_id="m_pending",
        status="paused",
        cloud_enabled=True,
    )
    payload = state.to_dict()
    payload["finalization"] = {"status": "pending", "message": ""}
    meeting = {
        "id": "m_pending",
        "cloud_enabled": True,
        "state_json": json.dumps(payload),
    }
    repo = FakeRepository()

    _mark_ended(repo, meeting)

    patched = json.loads(repo.updates[0][1]["state_json"])
    assert patched["finalization"]["status"] == "unavailable"


def test_mark_ended_cloud_off_becomes_disabled():
    state = MeetingState(
        meeting_id="m_off",
        status="active",
        cloud_enabled=False,
    )
    payload = state.to_dict()
    payload["finalization"] = {"status": "pending", "message": ""}
    meeting = {
        "id": "m_off",
        "cloud_enabled": False,
        "state_json": json.dumps(payload),
    }
    repo = FakeRepository()

    _mark_ended(repo, meeting)

    patched = json.loads(repo.updates[0][1]["state_json"])
    assert patched["finalization"]["status"] == "disabled"


def test_pid_alive_rejects_missing_and_dead_pids():
    assert _pid_alive(None) is False
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(2**31 - 1) is False


def test_is_session_dead_uses_heartbeat_then_pid():
    assert is_session_dead({}) is True
    assert is_session_dead({"app_heartbeat_at": None}) is True
    assert is_session_dead({"app_heartbeat_at": "not-a-timestamp"}) is True

    stale = {
        "app_heartbeat_at": _iso_age(STALE_HEARTBEAT_S + 5),
        "app_pid": os.getpid(),
    }
    assert is_session_dead(stale) is True

    live = {
        "app_heartbeat_at": _iso_age(5),
        "app_pid": os.getpid(),
    }
    assert is_session_dead(live) is False

    fresh_dead_pid = {
        "app_heartbeat_at": _iso_age(5),
        "app_pid": 2**31 - 1,
    }
    assert is_session_dead(fresh_dead_pid) is True


def test_find_recoverable_meetings_keeps_only_dead_sessions():
    repo = FakeRepository()
    repo.interrupted = [
        {"id": "m_live", "app_heartbeat_at": _iso_age(5), "app_pid": os.getpid()},
        {"id": "m_dead", "app_heartbeat_at": _iso_age(STALE_HEARTBEAT_S + 10),
         "app_pid": os.getpid()},
        {"id": "m_no_beat"},
    ]

    recovered = find_recoverable_meetings(repo)

    assert [m["id"] for m in recovered] == ["m_dead", "m_no_beat"]


def test_find_recoverable_meetings_skips_deferred_cards():
    repo = FakeRepository()
    state = MeetingState(
        meeting_id="m_kept",
        status="needs_recovery",
        cloud_enabled=True,
    )
    payload = state.to_dict()
    payload["finalization"] = {
        "status": "unavailable",
        "message": "ASR unfinished",
        "card_deferred": True,
    }
    repo.interrupted = [
        {
            "id": "m_kept",
            "status": "needs_recovery",
            "state_json": json.dumps(payload),
        },
        {"id": "m_crash"},
    ]

    recovered = find_recoverable_meetings(repo)

    assert [m["id"] for m in recovered] == ["m_crash"]


def test_find_recoverable_meetings_returns_empty_on_scan_failure():
    repo = FakeRepository()
    repo.fail_list = True
    assert find_recoverable_meetings(repo) == []


def test_finalize_meeting_rejects_missing_id():
    assert finalize_meeting(FakeRepository(), {}) is False


def test_finalize_meeting_returns_false_when_pending_chunks_cannot_load():
    repo = FakeRepository()
    repo.fail_pending = True
    assert finalize_meeting(repo, {"id": "m_crash"}) is False
    assert repo.updates == []


def test_finalize_meeting_with_no_pending_chunks_marks_ended():
    repo = FakeRepository()
    meeting = {"id": "m_empty", "cloud_enabled": False}

    assert finalize_meeting(repo, meeting) is True
    assert repo.reset_calls == ["m_empty"]
    assert repo.updates
    assert repo.updates[-1][0] == "m_empty"
    assert repo.updates[-1][1]["status"] == "ended"


def test_finalize_meeting_transcribes_pending_chunks(monkeypatch):
    FakeAsrEngine.instances = []
    monkeypatch.setattr("meeting.asr.engine.MeetingAsrEngine", FakeAsrEngine)

    repo = FakeRepository()
    repo.pending = [_pending_chunk()]
    repo.unfinished = 0
    meeting = {"id": "m_crash", "asr_model": "base"}
    progress = []

    ok = finalize_meeting(
        repo, meeting, asr_language="en",
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert ok is True
    assert len(FakeAsrEngine.instances) == 1
    engine = FakeAsrEngine.instances[0]
    assert engine.is_available is True
    assert engine.started is True
    assert engine.stopped is True
    assert engine.language == "en"
    assert len(engine.enqueued) == 1
    chunk = engine.enqueued[0]
    assert isinstance(chunk, SpooledChunk)
    assert chunk.chunk_id == 7
    assert chunk.meeting_id == "m_crash"
    assert repo.commits == [("m_crash", 7, [])]
    assert repo.updates[-1][1]["status"] == "ended"
    assert progress[-1] == (1, 1)


def test_finalize_meeting_unavailable_asr_leaves_session_recoverable(monkeypatch):
    class UnavailableAsr(FakeAsrEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.is_available = False

    FakeAsrEngine.instances = []
    monkeypatch.setattr("meeting.asr.engine.MeetingAsrEngine", UnavailableAsr)

    repo = FakeRepository()
    repo.pending = [_pending_chunk()]
    meeting = {"id": "m_crash"}

    assert finalize_meeting(repo, meeting) is False
    assert FakeAsrEngine.instances[0].stopped is True
    assert repo.updates == []


def test_finalize_meeting_drain_timeout_does_not_mark_ended(monkeypatch):
    class SlowAsr(FakeAsrEngine):
        def drain(self, timeout):
            return False

    FakeAsrEngine.instances = []
    monkeypatch.setattr("meeting.asr.engine.MeetingAsrEngine", SlowAsr)

    repo = FakeRepository()
    repo.pending = [_pending_chunk()]
    meeting = {"id": "m_crash"}

    assert finalize_meeting(repo, meeting) is False
    assert FakeAsrEngine.instances[0].stopped is True
    assert all(fields.get("status") != "ended" for _, fields in repo.updates)


def test_finalize_meeting_unfinished_chunks_mark_needs_recovery(monkeypatch):
    FakeAsrEngine.instances = []
    monkeypatch.setattr("meeting.asr.engine.MeetingAsrEngine", FakeAsrEngine)

    repo = FakeRepository()
    repo.pending = [_pending_chunk()]
    repo.unfinished = 1
    meeting = {"id": "m_crash"}

    assert finalize_meeting(repo, meeting) is False
    assert repo.updates[-1] == ("m_crash", {"status": "needs_recovery"})


def _create_meeting(repo, spool_dir, meeting_id="m_reconcile", status="active"):
    repo.create_meeting(
        id=meeting_id,
        title="Interrupted capture",
        status=status,
        started_at=datetime.now().isoformat(),
        host_token="host",
        guest_token="guest",
        cloud_enabled=False,
        spool_dir=str(spool_dir),
    )
    return repo.get_meeting(meeting_id)


def _write_wav(path, seconds=1.0, rate=16000):
    samples = np.arange(int(seconds * rate), dtype=np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(samples.tobytes())


def test_reconcile_registers_orphan_wav_once(repo, tmp_path):
    from meeting.capture.spool import (
        chunk_recovery_meta_path,
        write_chunk_recovery_meta,
    )

    meeting = _create_meeting(repo, tmp_path)
    canonical = tmp_path / "mic_00000.wav"
    orphan = tmp_path / "mic_00000.wav.orphan"
    _write_wav(canonical, seconds=1.25)
    os.replace(canonical, orphan)
    write_chunk_recovery_meta(
        str(orphan), meeting["id"], "mic", 0, 2.5, 1.25
    )

    first = reconcile_meeting_audio(repo, meeting)
    second = reconcile_meeting_audio(repo, meeting)

    rows = repo.get_audio_chunks(meeting["id"])
    assert first == {"orphan_chunks": 1, "tail_chunks": 0}
    assert second == {"orphan_chunks": 0, "tail_chunks": 0}
    assert len(rows) == 1
    assert rows[0]["seq"] == 0
    assert rows[0]["start_s"] == 2.5
    assert rows[0]["duration_s"] == 1.25
    assert rows[0]["file_path"] == str(canonical)
    assert canonical.is_file()
    assert not orphan.exists()
    assert not os.path.exists(chunk_recovery_meta_path(str(canonical)))


def test_reconcile_sidecar_after_db_commit_does_not_duplicate(repo, tmp_path):
    from meeting.capture.spool import write_chunk_recovery_meta

    meeting = _create_meeting(repo, tmp_path)
    wav_path = tmp_path / "mic_00000.wav"
    _write_wav(wav_path)
    repo.register_chunk(
        meeting_id=meeting["id"], channel="mic", seq=0,
        file_path=str(wav_path), start_s=0.0, duration_s=1.0,
        sample_rate=16000, asr_status="pending",
    )
    write_chunk_recovery_meta(
        str(wav_path), meeting["id"], "mic", 0, 0.0, 1.0
    )

    result = reconcile_meeting_audio(repo, meeting)

    assert result == {"orphan_chunks": 0, "tail_chunks": 0}
    assert len(repo.get_audio_chunks(meeting["id"])) == 1


def test_reconcile_materializes_only_unregistered_pcm_tail(repo, tmp_path):
    from meeting.capture.spool import write_session_meta

    meeting = _create_meeting(repo, tmp_path)
    source = np.arange(2 * 16000, dtype=np.int16)
    pcm_path = tmp_path / "mic_session.pcm"
    pcm_path.write_bytes(source.tobytes())
    write_session_meta(
        str(tmp_path / "mic_session.json"),
        16000,
        source.size,
        0.0,
        pcm_origin_s=0.0,
    )
    first_wav = tmp_path / "mic_00000.wav"
    _write_wav(first_wav)
    repo.register_chunk(
        meeting_id=meeting["id"], channel="mic", seq=0,
        file_path=str(first_wav), start_s=0.0, duration_s=1.0,
        sample_rate=16000, asr_status="done",
    )

    first = reconcile_meeting_audio(repo, meeting)
    second = reconcile_meeting_audio(repo, meeting)

    rows = repo.get_audio_chunks(meeting["id"])
    assert first == {"orphan_chunks": 0, "tail_chunks": 1}
    assert second == {"orphan_chunks": 0, "tail_chunks": 0}
    assert len(rows) == 2
    recovered = rows[1]
    assert recovered["seq"] == 1
    assert recovered["start_s"] == 1.0
    assert recovered["duration_s"] == 1.0
    assert recovered["asr_status"] == "pending"
    with wave.open(recovered["file_path"], "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnframes() == 16000


def test_reconcile_ignores_raw_file_already_converted_into_prefix(repo, tmp_path):
    from meeting.capture.spool import write_session_meta

    meeting = _create_meeting(repo, tmp_path)
    samples = np.arange(16000, dtype=np.int16)
    (tmp_path / "mic_session.16k.pcm").write_bytes(samples.tobytes())
    # Simulate Windows refusing the best-effort unlink after conversion.
    (tmp_path / "mic_session.pcm").write_bytes(samples.tobytes())
    write_session_meta(
        str(tmp_path / "mic_session.json"),
        16000,
        samples.size,
        0.0,
        pcm_origin_s=0.0,
        prefix_16k_samples=samples.size,
        pcm_active=False,
    )

    result = reconcile_meeting_audio(repo, meeting)

    rows = repo.get_audio_chunks(meeting["id"])
    assert result == {"orphan_chunks": 0, "tail_chunks": 1}
    assert len(rows) == 1
    assert rows[0]["duration_s"] == 1.0


def test_startup_scan_discovers_terminal_meeting_with_stranded_wav(repo, tmp_path):
    from meeting.capture.spool import write_chunk_recovery_meta

    meeting = _create_meeting(repo, tmp_path, status="ended")
    canonical = tmp_path / "loopback_00000.wav"
    orphan = tmp_path / "loopback_00000.wav.orphan"
    _write_wav(canonical, seconds=0.5)
    os.replace(canonical, orphan)
    write_chunk_recovery_meta(
        str(orphan), meeting["id"], "loopback", 0, 4.0, 0.5
    )

    recovered = find_recoverable_meetings(repo)

    assert [row["id"] for row in recovered] == [meeting["id"]]
    chunks = repo.get_audio_chunks(meeting["id"])
    assert len(chunks) == 1
    assert chunks[0]["asr_status"] == "pending"
