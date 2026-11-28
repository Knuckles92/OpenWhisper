"""Regression tests for meeting pipeline performance and polish fixes."""
from __future__ import annotations

import queue
import threading
import wave
from types import SimpleNamespace

import numpy as np

from meeting.asr import offline as offline_mod
from meeting.asr import preview as preview_mod
from meeting.asr.engine import MeetingAsrEngine
from meeting.capture.health import CaptureSignalMonitor
from meeting.capture.spool import TARGET_RATE, concat_channel_chunks_to_wav
from meeting.capture_session import MeetingCapture
from meeting.engine import MeetingEngine, MeetingEngineOptions
from meeting.interfaces import CHANNEL_MIC, CaptureBlock
from meeting.live_signals import LiveSignals
from meeting.refinalize import _RedecodeBackend
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore


def test_offline_cut_ranges_slices_bounded_window(monkeypatch):
    """offline_cut_ranges must not pass the entire remaining meeting to find_cut_point."""
    rate = 16000
    total = rate * 180  # 3 minutes
    audio = np.zeros(total, dtype=np.int16)
    seen_lengths = []
    real_find = offline_mod.find_cut_point

    def spying_find(buf, sample_rate, **kwargs):
        seen_lengths.append(len(buf))
        return real_find(buf, sample_rate, **kwargs)

    monkeypatch.setattr(offline_mod, "find_cut_point", spying_find)
    ranges = offline_mod.offline_cut_ranges(
        total, rate, audio, target_sec=15.0, max_sec=25.0, quiet_window_s=0.7
    )
    assert ranges and ranges[0][0] == 0 and ranges[-1][1] == total
    max_allowed = int(25.0 * rate) + 1
    assert all(n <= max_allowed for n in seen_lengths)


def test_ephemeral_capture_counters_do_not_trigger_sqlite_persist():
    """Incrementing signal_windows updates in-memory state + status without persist_state."""
    persist_calls = []

    class SpyRepo:
        state_metadata_only = True

        def persist_state(self, meeting_id, state):
            persist_calls.append(dict(state["capture"]))
            return state

    store = MeetingStateStore(MeetingState("m_ephem"), repository=SpyRepo())
    emitted = []
    monitor = CaptureSignalMonitor()
    monitor.reset_stream(now=0.0)
    source = SimpleNamespace(channel=CHANNEL_MIC, is_active=lambda: True)

    capture = MeetingCapture(
        MeetingEngineOptions(),
        SimpleNamespace(is_paused=False, meeting_time=lambda t: t),
        SpyRepo(),
        recording=lambda: True,
        asr=lambda: None,
        store=lambda: store,
        on_chunk=lambda c: None,
        emit_status=lambda note=None: emitted.append(note),
        emit=lambda kind, payload: None,
    )
    capture.sources = [source]
    capture._monitors = {CHANNEL_MIC: monitor}

    # Initial update changes durable fields (mic_available False -> True): must persist.
    capture.update_status()
    assert len(persist_calls) == 1
    assert len(emitted) == 1

    # Signal windows increment: ephemeral only -> no persist_state, still broadcasts.
    import time as _time
    base = _time.monotonic()
    for offset in (0.0, 0.25, 0.5):
        monitor.observe(
            CaptureBlock(CHANNEL_MIC, np.full(4000, 5000, dtype=np.int16), 16000, base + offset),
            now=base + offset,
        )
    capture.update_status()
    assert len(persist_calls) == 1
    assert len(emitted) == 2
    assert store.snapshot()["capture"]["mic_signal_windows"] == 1
    assert store.snapshot()["capture"]["mic_receiving"] is True

    # Durable field change (audio_incomplete / integrity_error): must persist.
    capture.integrity_error = "Storage fell behind."
    capture.update_status()
    assert len(persist_calls) == 2
    assert store.snapshot()["capture"]["audio_incomplete"] is True


def test_supports_offline_redecode_reflects_backend_model():
    """Non-Whisper backends without .model advertise supports_offline_redecode=False."""
    asr = MeetingAsrEngine.__new__(MeetingAsrEngine)
    asr.is_available = True
    asr._backend = SimpleNamespace(is_available=lambda: True)  # no .model
    assert asr.supports_offline_redecode is False

    engine = MeetingEngine.__new__(MeetingEngine)
    engine.options = MeetingEngineOptions(end_redecode=True)
    engine._asr = asr
    assert engine._will_redecode_at_end() is False

    asr._backend = SimpleNamespace(is_available=lambda: True, model=object())
    assert asr.supports_offline_redecode is True
    assert engine._will_redecode_at_end() is True

    redecode_backend = _RedecodeBackend(None)
    redecode_backend.backend = SimpleNamespace(is_available=lambda: True)
    result = redecode_backend.unavailable_result()
    assert result is not None and result.get("ok") is False and result.get("unsupported") is True


def test_speech_previews_resample_per_decode_tick_not_per_block(monkeypatch):
    """Both preview workers buffer native blocks and resample once per decode/stream tick."""
    resample_calls = []
    real_prepare = preview_mod.prepare_for_whisper

    def spying_prepare(frames, rate):
        resample_calls.append((len(frames), rate))
        return real_prepare(frames, rate)

    monkeypatch.setattr(preview_mod, "prepare_for_whisper", spying_prepare)

    # 1) WindowSpeechPreview: feed 50 x 1024-sample blocks at 48 kHz (~1.07s), decode once.
    worker = preview_mod.WindowSpeechPreview.__new__(preview_mod.WindowSpeechPreview)
    events = []
    worker.backend = SimpleNamespace(preview_audio=lambda samples, *a, **kw: {"text": "hi"})
    worker.callback, worker.busy, worker.language = events.append, (lambda: False), "auto"
    worker._sessions, worker._next_decode = {}, 0.0
    worker._stop = threading.Event()
    worker._queue = queue.Queue(maxsize=64)

    block_dur = 1024 / 48000.0
    for i in range(50):
        blk = CaptureBlock("mic", np.full(1024, 1000, dtype=np.int16), 48000, 10.0 + i * block_dur)
        worker._accept(blk, i * block_dur)
    assert len(resample_calls) == 0
    worker._decode()
    assert len(resample_calls) == 1
    assert resample_calls[0] == (50 * 1024, 48000)
    assert len(events) == 1

    # 2) MeetingSpeechPreview: feed 36 x 1024-sample blocks at 48 kHz (~0.768s >= 0.75s).
    resample_calls.clear()
    stream_calls = []
    streamer = preview_mod.MeetingSpeechPreview.__new__(preview_mod.MeetingSpeechPreview)
    streamer.backend = SimpleNamespace(
        stream_audio=lambda ch, samples, lang, finish=False: stream_calls.append(len(samples)) or []
    )
    streamer.callback, streamer.busy, streamer.language = events.append, (lambda: False), "auto"
    streamer._stop = threading.Event()
    sessions = {}
    for i in range(36):
        blk = CaptureBlock("mic", np.full(1024, 1000, dtype=np.int16), 48000, 20.0 + i * block_dur)
        streamer._handle_block(sessions, blk, i * block_dur)
    assert len(resample_calls) == 1
    assert len(stream_calls) == 1


def test_live_signals_checks_transcript_outside_store_lock(monkeypatch):
    """_transcript_unchanged must run before acquiring MeetingStateStore._lock."""
    monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled", lambda _: True)
    store = MeetingStateStore(MeetingState("m_lock", cloud_enabled=True))
    in_lock = [False]
    observed_lock_states = []

    orig_with_state = store.with_state

    def tracking_with_state(fn):
        in_lock[0] = True
        try:
            return orig_with_state(fn)
        finally:
            in_lock[0] = False

    store.with_state = tracking_with_state

    row = {"id": "sg_1", "text": "Ship the release Friday.", "start_s": 10.0, "end_s": 14.0}

    class TrackingRepo:
        def get_segments(self, mid, **kwargs):
            observed_lock_states.append(("get_segments", in_lock[0]))
            return [dict(row)]

        def get_segment(self, mid, sid):
            observed_lock_states.append(("get_segment", in_lock[0]))
            return dict(row) if sid == "sg_1" else None

    judge = SimpleNamespace(
        ask=lambda s, q: {"decision": {"noul": 0.95}, "decision_anchor": {"choice": "sg_1"}}
    )
    worker = LiveSignals(store, TrackingRepo(), judge, lambda: True)
    worker._run(0)
    worker.shutdown()

    assert observed_lock_states
    assert all(held is False for _op, held in observed_lock_states)
    assert len(store.snapshot()["live_highlights"]) == 1


def test_concat_channel_chunks_to_wav_chunks_long_silence_gap(tmp_path):
    """concat_channel_chunks_to_wav bridges multi-second gaps without a single giant allocation."""
    wav1 = tmp_path / "mic_00000.wav"
    wav2 = tmp_path / "mic_00001.wav"
    for p in (wav1, wav2):
        with wave.open(str(p), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TARGET_RATE)
            wf.writeframes(np.full(TARGET_RATE, 100, dtype=np.int16).tobytes())

    out = tmp_path / "mic_session.wav"
    chunks = [
        {"id": 1, "channel": "mic", "seq": 0, "start_s": 0.0, "file_path": str(wav1)},
        {"id": 2, "channel": "mic", "seq": 1, "start_s": 6.5, "file_path": str(wav2)},
    ]
    origin = concat_channel_chunks_to_wav(chunks, "mic", str(out))
    assert origin == 0.0
    with wave.open(str(out), "rb") as wf:
        # 1.0s chunk1 + 5.5s silence gap + 1.0s chunk2 = 7.5s
        assert wf.getnframes() == int(round(7.5 * TARGET_RATE))


def test_spool_finalize_session_streams_directly_to_wav_without_16k_pcm(tmp_path):
    """Normal flush stream-resamples native PCM directly into session WAV without .16k.pcm."""
    from meeting.capture.spool import SpoolWriter, session_wav_path
    from meeting.clock import MeetingClock

    class MinimalRepo:
        def __init__(self):
            self._id = 0

        def register_chunk(self, **fields):
            self._id += 1
            return self._id

    clock = MeetingClock()
    clock.start()
    spool_dir = str(tmp_path / "spool")
    writer = SpoolWriter(
        "m_direct", "mic", spool_dir, clock, MinimalRepo(),
        on_chunk=lambda c: None, target_sec=10.0, max_sec=20.0,
    )
    # Feed 1.0s of 48 kHz audio.
    import time as _time
    writer.feed(CaptureBlock("mic", np.full(48000, 1000, dtype=np.int16), 48000, _time.monotonic()))
    writer.flush(timeout_s=10.0)

    wav_path = session_wav_path(spool_dir, "mic")
    assert (tmp_path / "spool" / "mic_session.wav").is_file()
    assert not (tmp_path / "spool" / "mic_session.16k.pcm").exists()
    assert not (tmp_path / "spool" / "mic_session.pcm").exists()
    with wave.open(wav_path, "rb") as wf:
        assert wf.getframerate() == TARGET_RATE
        assert wf.getnframes() == TARGET_RATE


def test_capture_flush_runs_spools_concurrently():
    """MeetingCapture.flush() flushes mic and loopback spools concurrently."""
    barrier = threading.Barrier(2, timeout=2.0)

    class BarrierSpool:
        health_error = None

        def flush(self):
            barrier.wait()
            return None

    capture = MeetingCapture(
        MeetingEngineOptions(),
        SimpleNamespace(is_paused=False, meeting_time=lambda t: t),
        None,
        recording=lambda: True,
        asr=lambda: None,
        store=lambda: None,
        on_chunk=lambda c: None,
        emit_status=lambda note=None: None,
        emit=lambda kind, payload: None,
    )
    capture.spools = {"mic": BarrierSpool(), "loopback": BarrierSpool()}
    capture.flush()  # Would raise BrokenBarrierError if executed sequentially.
    assert capture.integrity_error is None


def test_reconcile_skips_clean_ended_spool_without_db_queries(tmp_path):
    """reconcile_meeting_audio skips get_audio_chunks when spool has no crash artifacts."""
    from meeting.recovery import reconcile_meeting_audio

    spool_dir = tmp_path / "clean_spool"
    spool_dir.mkdir()
    # A clean finished spool has session WAV and chunk WAVs, no .recovery.json or raw PCM.
    (spool_dir / "mic_session.wav").write_bytes(b"RIFF" + b"\x00" * 100)

    calls = []

    class SpyRepo:
        def get_audio_chunks(self, meeting_id):
            calls.append(meeting_id)
            return []

    result = reconcile_meeting_audio(
        SpyRepo(), {"id": "m_clean", "status": "ended", "spool_dir": str(spool_dir)}
    )
    assert result == {"orphan_chunks": 0, "tail_chunks": 0}
    assert calls == []


def test_sweep_line_match_live_to_offline_handles_nested_and_multi_channel():
    """Sweep-line matching pairs overlapping segments accurately across channels."""
    from meeting.interfaces import TranscriptSegment
    from meeting.persist.repository import _match_live_to_offline

    old_items = [
        {"id": "sg_o1", "channel": "mic", "start_s": 0.0, "end_s": 10.0,
         "pinned": True, "speaker_participant_id": "p_me", "speaker_source": "human"},
        {"id": "sg_o2", "channel": "mic", "start_s": 2.0, "end_s": 4.0,
         "pinned": False, "speaker_participant_id": "p_me", "speaker_source": "channel"},
        {"id": "sg_o3", "channel": "loopback", "start_s": 1.0, "end_s": 3.0,
         "pinned": True, "speaker_participant_id": "p_alice", "speaker_source": "human"},
    ]
    new_items = [
        TranscriptSegment("sg_n1", "m1", None, "mic", 0.2, 9.8, "long"),
        TranscriptSegment("sg_n2", "m1", None, "mic", 2.0, 4.1, "nested"),
        TranscriptSegment("sg_n3", "m1", None, "loopback", 1.1, 3.0, "alice"),
    ]
    id_map, used_old = _match_live_to_offline(old_items, new_items)
    assert id_map == {"sg_o1": "sg_n1", "sg_o2": "sg_n2", "sg_o3": "sg_n3"}
    assert used_old == {0, 1, 2}
    assert new_items[0].speaker_pinned is True
    assert new_items[0].speaker_participant_id == "p_me"
    assert new_items[2].speaker_pinned is True
    assert new_items[2].speaker_participant_id == "p_alice"


def test_end_run_preserves_redecode_steps_when_cloud_is_off():
    """When will_offline=True and cloud_enabled=False, finalization keeps redecode step status."""
    from meeting.end_run import EndRun, INSIGHTS_OFF_MESSAGE

    recorded = []

    def set_fin(status, message="", **kwargs):
        recorded.append((status, message, kwargs))
        return True

    end = EndRun(
        "m_redecode_off",
        complete=True,
        unfinished=0,
        scheduler=None,
        will_offline=True,
        will_speaker_id=False,
        set_finalization=set_fin,
        redecode=lambda cb: True,
        redecode_failure=lambda: "",
        identify_speakers=lambda **kw: {},
        allow_agent_writes=lambda: None,
        revoke_agent_writes=lambda: None,
        core_is_healthy=lambda: False,
        offline_message=lambda: "",
        summary_stats=lambda: {},
    )
    end.plan(cloud_enabled=False, want_polish=False, want_report=False)
    end.redecode()
    end.insights()

    final_status, final_msg, final_kw = recorded[-1]
    assert final_status == "disabled"
    assert final_msg == INSIGHTS_OFF_MESSAGE
    steps = final_kw.get("steps") or []
    assert [s["id"] for s in steps] == ["redecode", "finalize"]
    assert all(s["status"] == "completed" for s in steps)


def test_engine_shutdown_cancels_agent_and_bounds_join_timeout():
    """MeetingEngine.shutdown() cancels active agent core and bounds join to SHUTDOWN_JOIN_TIMEOUT_S."""
    from meeting.engine import SHUTDOWN_JOIN_TIMEOUT_S

    engine = MeetingEngine.__new__(MeetingEngine)
    engine._shutting_down = False
    engine._processing_stop = threading.Event()
    engine._agent_writes_allowed = True
    engine._active = False
    engine._lifecycle_lock = threading.RLock()
    canceled = []
    engine._agent_core = SimpleNamespace(cancel=lambda: canceled.append(True), shutdown=lambda: None)
    engine._initializing_agent_core = None
    join_timeouts = []
    engine._end_thread = SimpleNamespace(join=lambda timeout=None: join_timeouts.append(timeout))
    engine._fast_features_lock = threading.RLock()
    engine._fast_features_stopped = False
    engine._server = None
    engine.store = None
    engine._processing_thread = None
    engine._diarizer_thread = None
    engine._hb_stop = None

    engine.shutdown()

    assert engine._shutting_down is True
    assert engine._agent_writes_allowed is False
    assert canceled == [True]
    assert join_timeouts == [SHUTDOWN_JOIN_TIMEOUT_S]
    assert SHUTDOWN_JOIN_TIMEOUT_S <= 15.0

