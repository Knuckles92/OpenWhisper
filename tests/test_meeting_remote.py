"""Meeting audio through an actual paired TLS host, with fake speech inference."""
import json
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from meeting.asr.engine import MeetingAsrEngine
from meeting.asr.remote import (
    MeetingRemoteBackend, RemoteMeetingUnavailable, remote_route, saved_remote_route,
)
from meeting.interfaces import SpooledChunk, TranscriptSegment
from services.remote_asr import settings as remote_settings
from services.settings import SettingsKey, resolve_meeting_asr_source
from tests.helpers import write_wav
from tests import test_remote_engine as remote_tests
from tests.test_remote_engine import _pair, _tone, _wait_for

# Register the shared real-TLS fixtures in this test module.
engine = remote_tests.engine
host = remote_tests.host
identity = remote_tests.identity
no_real_tailscale = remote_tests.no_real_tailscale
store = remote_tests.store


@pytest.fixture
def paired(host):
    remote_settings.save_client_pairing("127.0.0.1", host.port, _pair(host))
    return remote_route()


def create_meeting(repo, tmp_path, route=None):
    repo.create_meeting(
        id="m_remote", title="Remote meeting", status="active", started_at="2026-09-27T12:00:00Z",
        host_token="host", guest_token="guest", cloud_enabled=False, spool_dir=str(tmp_path),
        asr_model="base", asr_remote_json=json.dumps(route) if route else None,
    )


def chunk(repo, tmp_path, channel="mic", start=0):
    path = tmp_path / f"{channel}-{start}.wav"
    write_wav(path, duration_s=1)
    fields = dict(meeting_id="m_remote", channel=channel, seq=int(start),
                  file_path=str(path), start_s=float(start), duration_s=1., sample_rate=16000)
    chunk_id = repo.register_chunk(**fields)
    return SpooledChunk(chunk_id=chunk_id, **fields)


def test_speech_source_defaults_local_and_does_not_follow_dictation():
    assert resolve_meeting_asr_source({}) == "local"
    assert resolve_meeting_asr_source({SettingsKey.SELECTED_MODEL: "remote"}) == "local"
    assert resolve_meeting_asr_source({SettingsKey.MEETING_ASR_SOURCE: "bad"}) == "local"
    assert resolve_meeting_asr_source({SettingsKey.MEETING_ASR_SOURCE: "remote"}) == "remote"


def test_unpaired_remote_has_actionable_error():
    with pytest.raises(ValueError, match="Pair a computer"):
        remote_route({})


@pytest.mark.parametrize("raw", ["", "{}", "null", "broken", '{"fingerprint":"host"}'])
def test_corrupt_saved_route_never_falls_back_to_local(raw):
    with pytest.raises(ValueError, match="configuration is invalid"):
        saved_remote_route({"asr_remote_json": raw})
    assert saved_remote_route({}) is None


def test_live_mic_and_system_audio_use_remote_model_and_meeting_timestamps(paired, engine, repo, tmp_path, monkeypatch):
    create_meeting(repo, tmp_path)
    local = Mock(side_effect=AssertionError("Must not load a local speech model"))
    monkeypatch.setattr("transcriber.local_backend.LocalWhisperBackend", local)
    asr = MeetingAsrEngine("large-v3", "m_remote", repo, language="de", remote=paired)
    assert asr.is_available, asr.last_error
    asr.start(lambda c, segments: repo.commit_chunk_transcription("m_remote", c.chunk_id, segments))
    try:
        asr.enqueue(chunk(repo, tmp_path, "mic", 10))
        asr.enqueue(chunk(repo, tmp_path, "loopback", 20))
        assert asr.drain(5)
        segments = repo.get_segments("m_remote")
        assert [(s["channel"], s["start_s"], s["end_s"]) for s in segments] == [
            ("mic", 10., 11.), ("loopback", 20., 21.)]
        assert all(call[2] == "de" for call in engine.calls)
        assert repo.count_unfinished_chunks("m_remote") == 0
        assert asr._backend.route["model"] == engine.model
        local.assert_not_called()
    finally:
        asr.stop()


def test_host_change_and_new_pairing_are_rejected_before_audio(paired, host, engine, monkeypatch):
    backend = MeetingRemoteBackend(paired)
    backend.reload_model()
    original_route = dict(backend.route)
    try:
        engine.model = "a-different-model"
        host.engine_changed()
        assert _wait_for(lambda: not backend._process.alive)
        with pytest.raises(RemoteMeetingUnavailable):
            backend.ensure_ready()
        assert not engine.calls
        pairing = remote_settings.load_client_pairing()
        monkeypatch.setattr(remote_settings, "load_client_pairing", lambda: replace(pairing, fingerprint="different"))
        with pytest.raises(RemoteMeetingUnavailable, match="different computer"):
            backend.ensure_ready()
        assert backend.route == original_route
        assert not engine.calls
    finally:
        backend.cleanup()


def test_host_restart_recovers_without_switching_model(paired, host, engine):
    backend = MeetingRemoteBackend(paired)
    backend.reload_model()
    model = backend.model
    port = host.port
    host.stop()
    host.start(port=port, bind="127.0.0.1")
    try:
        segments, _ = model.transcribe(_tone(16000), language="fr")
        assert list(segments)[0].text == "heard 16000"
        assert engine.calls[-1][2] == "fr"
        assert backend.is_available()
    finally:
        backend.cleanup()


def test_remote_outage_keeps_chunks_pending_and_stop_is_prompt(paired, repo, tmp_path, monkeypatch):
    create_meeting(repo, tmp_path)
    status = []
    asr = MeetingAsrEngine("auto", "m_remote", repo, remote=paired,
                           on_connection_status=lambda message, online: status.append((message, online)))
    monkeypatch.setattr(asr._backend, "ensure_ready", Mock(side_effect=RemoteMeetingUnavailable("Host offline")))
    asr.start(lambda *_: pytest.fail("No transcript while offline"))
    pending = chunk(repo, tmp_path)
    asr.enqueue(pending)
    assert _wait_for(lambda: bool(status))
    assert status[-1][1] is False
    assert repo.get_pending_chunks("m_remote")[0]["asr_attempts"] == 0
    stopped = threading.Event()
    thread = threading.Thread(target=lambda: (asr.stop(), stopped.set()))
    thread.start()
    assert stopped.wait(2)
    thread.join()
    assert repo.count_unfinished_chunks("m_remote") == 1


def test_live_worker_resumes_pending_audio_after_host_returns(
    paired, host, engine, repo, tmp_path, monkeypatch,
):
    from meeting.asr import engine as asr_module

    monkeypatch.setattr(asr_module, "REMOTE_RETRY_BACKOFF_S", (0.1,))
    create_meeting(repo, tmp_path)
    status = []
    asr = MeetingAsrEngine("auto", "m_remote", repo, remote=paired,
                           on_connection_status=lambda message, online: status.append(online))
    port = host.port
    host.stop()
    asr.start(lambda c, segments: repo.commit_chunk_transcription("m_remote", c.chunk_id, segments))
    try:
        asr.enqueue(chunk(repo, tmp_path, "mic", 10))
        asr.enqueue(chunk(repo, tmp_path, "loopback", 20))
        assert _wait_for(lambda: False in status)
        assert repo.count_unfinished_chunks("m_remote") == 2
        host.start(port=port, bind="127.0.0.1")
        assert asr.drain(10)
        assert repo.count_unfinished_chunks("m_remote") == 0
        assert status[-1] is True
        assert len(engine.calls) == 2
    finally:
        asr.stop()


@pytest.mark.parametrize("family,model", [("parakeet", "parakeet-v3"), ("nemotron", "nemotron-3.5")])
def test_remote_preview_emits_text_for_host_model(paired, engine, repo, family, model):
    import numpy as np
    from meeting.interfaces import CaptureBlock
    engine.family, engine.model = family, model
    events = []
    asr = MeetingAsrEngine("auto", "m_remote", repo, remote=paired)
    try:
        asr.start_preview(events.append)
        asr.feed_preview(CaptureBlock("mic", np.full(16000, 1000, dtype=np.int16), 16000, 100), 0)
        assert _wait_for(lambda: bool(events))
        assert events[0]["channel"] == "mic"
        assert events[0]["text"]
        assert engine.calls[0][0] == ("stream" if family == "nemotron" else "transcribe")
    finally:
        asr.stop()


def test_unsupported_host_engine_is_not_used(paired, engine):
    engine.family, engine.model = "qwen_asr", "qwen3-asr-0.6b"
    backend = MeetingRemoteBackend(paired)
    try:
        backend.reload_model()
        assert not backend.is_available()
        assert "does not support meetings" in backend.last_error
        assert not engine.calls
    finally:
        backend.cleanup()


def test_whisper_host_needs_no_optional_catalog_preview(paired, engine, repo, tmp_path):
    engine.family, engine.model = "local_whisper", "base"
    asr = MeetingAsrEngine("auto", "m_remote", repo, remote=paired)
    try:
        assert asr.is_available
        asr.start_preview(lambda *_: None)
        assert asr._preview is None
    finally:
        asr.stop()


def test_remote_recovery_uses_saved_route_without_local_model_lease(paired, engine, repo, tmp_path):
    from meeting.recovery import finalize_meeting
    route = dict(paired, family=engine.family, model=engine.model, language="es")
    create_meeting(repo, tmp_path, route)
    chunk(repo, tmp_path)
    lease = Mock(side_effect=AssertionError("Remote recovery must not unload local models"))
    assert finalize_meeting(repo, repo.get_meeting("m_remote"), model_lease=(lease, lease))
    assert engine.calls[-1][2] == "es"
    assert repo.count_unfinished_chunks("m_remote") == 0


def test_remote_redecode_uses_saved_host_and_language(paired, engine, repo, tmp_path, monkeypatch):
    from meeting.refinalize import rerun_redecode
    route = dict(paired, family=engine.family, model=engine.model, language="fr")
    create_meeting(repo, tmp_path, route)

    def transcribe(model, spool_dir, meeting_id, chunks, language, progress_cb):
        segments, _ = model.transcribe(_tone(16000), language=language)
        return [TranscriptSegment(segment_id="sg_remote", meeting_id=meeting_id, chunk_id=None,
                                  channel="mic", start_s=0., end_s=1., text=list(segments)[0].text)]

    monkeypatch.setattr("meeting.asr.offline.transcribe_meeting_sessions", transcribe)
    lease = Mock(side_effect=AssertionError("Remote redecode must not unload local models"))
    result = rerun_redecode(repo, "m_remote", asr_model_name="large-v3", language="en", model_lease=(lease, lease))
    assert result["ok"], result
    assert engine.calls[-1][2] == "fr"
    assert repo.get_segments("m_remote")[0]["text"] == "heard 16000"


def test_engine_persists_actual_remote_route_before_capture(paired, engine, repo, tmp_path, monkeypatch):
    from meeting.engine import MeetingEngine, MeetingEngineOptions
    meeting = MeetingEngine(MeetingEngineOptions(
        asr_remote=paired, asr_language="de", spool_root=str(tmp_path),
        speaker_id_backend="off", end_polish=False, end_report=False,
    ), repository=repo)
    monkeypatch.setattr(meeting, "_start_server", lambda: "http://localhost/test")
    monkeypatch.setattr(meeting, "_start_fast_features", lambda: None)
    monkeypatch.setattr(meeting, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(meeting, "_start_diarizer", lambda: None)
    monkeypatch.setattr(meeting, "_maybe_start_intelligence", lambda: None)

    def capture():
        row = repo.get_meeting(meeting.meeting_id)
        assert row["asr_model"] == engine.model
        assert saved_remote_route(row) == dict(paired, family=engine.family, model=engine.model, language="de")
        assert meeting.store.snapshot()["speech"]["connected"] is True
        return "Ready"

    monkeypatch.setattr(meeting, "_start_capture", capture)
    try:
        meeting.start()
    finally:
        meeting.shutdown()


def test_unavailable_remote_aborts_before_recording(paired, engine, repo, tmp_path, monkeypatch):
    from meeting.engine import MeetingEngine, MeetingEngineOptions
    engine.available = False
    meeting = MeetingEngine(MeetingEngineOptions(asr_remote=paired, spool_root=str(tmp_path),
                                                speaker_id_backend="off"), repository=repo)
    capture = Mock()
    monkeypatch.setattr(meeting, "_start_capture", capture)
    monkeypatch.setattr(meeting, "_start_fast_features", lambda: None)
    monkeypatch.setattr(meeting, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(meeting, "_start_diarizer", lambda: None)
    with pytest.raises(RuntimeError, match="not loaded"):
        meeting.start()
    capture.assert_not_called()
    assert not engine.calls


def test_v12_migration_preserves_local_meetings_and_adds_remote_route(tmp_path):
    from services.database import DatabaseManager
    from meeting.persist.repository import SqlMeetingRepository
    path = str(tmp_path / "migration.db")
    db = DatabaseManager(path)
    repo = SqlMeetingRepository(db=db)
    create_meeting(repo, tmp_path)
    with db.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE meeting_sessions DROP COLUMN asr_remote_json")
        connection.exec_driver_sql("UPDATE schema_version SET version = 12")
    db.close()
    upgraded = DatabaseManager(path)
    try:
        row = SqlMeetingRepository(db=upgraded).get_meeting("m_remote")
        assert row["asr_remote_json"] is None
        assert row["title"] == "Remote meeting"
    finally:
        upgraded.close()
