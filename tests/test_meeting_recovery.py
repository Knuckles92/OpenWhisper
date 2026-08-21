"""Focused tests for Meeting Mode crash-recovery scan, finalize, and discard."""
import json
import os
from datetime import datetime, timedelta

from meeting.interfaces import SpooledChunk
from meeting.recovery import (
    STALE_HEARTBEAT_S,
    _mark_ended,
    _pid_alive,
    finalize_meeting,
    find_recoverable_meetings,
    is_session_dead,
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
