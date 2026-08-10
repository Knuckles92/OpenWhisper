"""
Tests for MeetingAsrEngine: fake backend, retry ×3, timestamped segments.
"""
import os
import sys
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.asr.engine import (
    FAST_MODE_BACKLOG_CHUNKS,
    MAX_ATTEMPTS,
    REVISE_MIN_ADVANCE_S,
    MeetingAsrEngine,
)
from meeting.interfaces import SpooledChunk


class FakeRepository:
    def __init__(self):
        self.statuses = []
        self.pending = []

    def set_chunk_status(self, chunk_id, status, error=None):
        self.statuses.append((chunk_id, status, error))

    def get_pending_chunks(self, meeting_id):
        return list(self.pending)


def _write_wav(path, duration_s=0.5, sample_rate=16000, amp=1000):
    frames = np.full(int(duration_s * sample_rate), amp, dtype=np.int16)
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(frames.tobytes())


def _make_engine(repo, backend):
    """Build an engine without loading a real Whisper model."""
    fake_cls = MagicMock(return_value=SimpleNamespace(
        is_available=lambda: False,
        is_model_missing=True,
        name="fake",
    ))
    with patch("transcriber.local_backend.LocalWhisperBackend", fake_cls):
        engine = MeetingAsrEngine("base", "m_test", repo)
    engine._backend = backend
    engine.is_available = True
    return engine


def _chunk(tmp_path, chunk_id=1, start_s=10.0):
    path = str(tmp_path / f"c{chunk_id}.wav")
    _write_wav(path)
    return SpooledChunk(
        chunk_id=chunk_id, meeting_id="m_test", channel="mic", seq=0,
        file_path=path, start_s=start_s, duration_s=0.5, sample_rate=16000,
    )


class FakeWhisperSeg:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class TestAsrSuccessPath:
    def test_emits_segments_with_meeting_timestamps(self, tmp_path):
        repo = FakeRepository()
        model = MagicMock()
        model.transcribe.return_value = (
            [FakeWhisperSeg(0.5, 1.5, "  hello world  "),
             FakeWhisperSeg(2.0, 3.0, "")],  # blank skipped
            SimpleNamespace(),
        )
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=model,
            cleanup=lambda: None,
        )
        engine = _make_engine(repo, backend)
        received = []
        def commit(chunk, segments):
            received.append(segments)
            repo.set_chunk_status(chunk.chunk_id, "done")

        engine.start(on_chunk_result=commit)
        try:
            engine.enqueue(_chunk(tmp_path, start_s=10.0))
            assert engine.drain(5.0)
            assert len(received) == 1
            segs = received[0]
            assert len(segs) == 1
            assert segs[0].text == "hello world"
            assert segs[0].start_s == pytest.approx(10.5)
            assert segs[0].end_s == pytest.approx(11.5)
            assert segs[0].meeting_id == "m_test"
            assert segs[0].channel == "mic"
            assert ("done" in [s[1] for s in repo.statuses])
            kwargs = model.transcribe.call_args.kwargs
            assert kwargs["beam_size"] == 5
            assert kwargs["condition_on_previous_text"] is False
        finally:
            engine.stop()

    def test_digital_silence_skips_whisper_but_commits_chunk(self, tmp_path):
        repo = FakeRepository()
        model = MagicMock()
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=model,
            cleanup=lambda: None,
        )
        engine = _make_engine(repo, backend)
        chunk = _chunk(tmp_path)
        _write_wav(chunk.file_path, amp=0)
        received = []

        def commit(done_chunk, segments):
            received.append((done_chunk, segments))
            repo.set_chunk_status(done_chunk.chunk_id, "done")

        engine.start(on_chunk_result=commit)
        try:
            engine.enqueue(chunk)
            assert engine.drain(5.0)
            model.transcribe.assert_not_called()
            assert received == [(chunk, [])]
            assert any(status == "done" for _, status, _ in repo.statuses)
        finally:
            engine.stop()

    def test_backlog_uses_fast_decode_until_queue_recovers(self, tmp_path):
        repo = FakeRepository()
        model = MagicMock(return_value=([], SimpleNamespace()))
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=model,
            cleanup=lambda: None,
        )
        engine = _make_engine(repo, backend)

        with engine._idle_cond:
            engine._outstanding = FAST_MODE_BACKLOG_CHUNKS + 1
        assert engine._beam_size_for_backlog() == 1

        with engine._idle_cond:
            engine._outstanding = 1
        assert engine._beam_size_for_backlog() == 5


class TestAsrRetry:
    def test_retries_three_times_then_gives_up(self, tmp_path):
        repo = FakeRepository()
        model = MagicMock()
        model.transcribe.side_effect = RuntimeError("boom")
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=model,
            cleanup=lambda: None,
        )
        engine = _make_engine(repo, backend)
        engine.start(on_chunk_result=lambda chunk, _: repo.set_chunk_status(
            chunk.chunk_id, "done"
        ))
        try:
            engine.enqueue(_chunk(tmp_path))
            assert engine.drain(10.0)
            assert model.transcribe.call_count == MAX_ATTEMPTS
            failed = [s for s in repo.statuses if s[1] == "failed"]
            assert len(failed) == MAX_ATTEMPTS
        finally:
            engine.stop()

    def test_succeeds_after_transient_failures(self, tmp_path):
        repo = FakeRepository()
        model = MagicMock()
        model.transcribe.side_effect = [
            RuntimeError("transient"),
            RuntimeError("transient"),
            ([FakeWhisperSeg(0.0, 1.0, "recovered")], SimpleNamespace()),
        ]
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=model,
            cleanup=lambda: None,
        )
        engine = _make_engine(repo, backend)
        received = []

        def commit(chunk, segments):
            received.append(segments)
            repo.set_chunk_status(chunk.chunk_id, "done")

        engine.start(on_chunk_result=commit)
        try:
            engine.enqueue(_chunk(tmp_path, start_s=5.0))
            assert engine.drain(10.0)
            assert model.transcribe.call_count == 3
            assert len(received) == 1
            assert received[0][0].text == "recovered"
            assert received[0][0].start_s == pytest.approx(5.0)
            assert any(s[1] == "done" for s in repo.statuses)
        finally:
            engine.stop()


class TestRollingReviseScheduling:
    def _engine(self):
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=MagicMock(),
            cleanup=lambda: None,
        )
        engine = _make_engine(FakeRepository(), backend)
        engine.revise_window = MagicMock(
            side_effect=lambda channel, frontier: {
                "channel": channel,
                "frontier": frontier,
                "items": [],
                "removed_ids": [],
            }
        )
        return engine

    def test_coalesces_until_minimum_progress_then_revises_latest(self):
        engine = self._engine()
        engine.schedule_revise("mic", 5.0)
        assert engine.run_pending_revises() == []
        engine.revise_window.assert_not_called()

        engine.schedule_revise("mic", REVISE_MIN_ADVANCE_S)
        results = engine.run_pending_revises()
        assert results[0]["frontier"] == REVISE_MIN_ADVANCE_S
        engine.revise_window.assert_called_once_with(
            "mic", REVISE_MIN_ADVANCE_S
        )

        engine.schedule_revise("mic", REVISE_MIN_ADVANCE_S + 5.0)
        assert engine.run_pending_revises() == []
        forced = engine.run_pending_revises(force=True)
        assert forced[0]["frontier"] == REVISE_MIN_ADVANCE_S + 5.0

    def test_due_channel_is_not_blocked_by_another_channels_partial_window(self):
        engine = self._engine()
        engine.schedule_revise("mic", 5.0)
        engine.schedule_revise("loopback", REVISE_MIN_ADVANCE_S)

        results = engine.run_pending_revises()

        assert [result["channel"] for result in results] == ["loopback"]
        assert engine._pending_revise == {"mic": 5.0}

    def test_live_revise_waits_behind_any_queued_draft(self):
        engine = self._engine()
        engine.schedule_revise("mic", REVISE_MIN_ADVANCE_S)
        with engine._idle_cond:
            # One in flight plus one queued behind it.
            engine._outstanding = 2

        assert engine.run_pending_revises() == []
        engine.revise_window.assert_not_called()

    def test_forced_revise_does_not_race_an_inflight_worker_decode(self):
        engine = self._engine()
        engine.schedule_revise("mic", 5.0)
        with engine._idle_cond:
            engine._outstanding = 1

        assert engine.run_pending_revises(force=True) == []
        engine.revise_window.assert_not_called()

    def test_revise_decodes_lead_in_without_upserting_context_only_rows(
        self, tmp_path
    ):
        path = str(tmp_path / "long.wav")
        _write_wav(path, duration_s=100.0)
        repo = FakeRepository()
        repo.get_audio_chunks = lambda _meeting_id: [{
            "id": 1,
            "channel": "mic",
            "seq": 0,
            "start_s": 0.0,
            "duration_s": 100.0,
            "file_path": path,
            "asr_status": "done",
        }]
        repo.get_segments = lambda _meeting_id, after_start_s=-1.0: []
        repo.get_segments_in_range = (
            lambda _meeting_id, _channel, _start_s, _end_s: []
        )
        persisted = {}

        def persist(_meeting_id, _channel, _start_s, _end_s,
                    segments, remove_ids):
            persisted["segments"] = segments
            persisted["remove_ids"] = remove_ids
            return [
                {"id": segment.segment_id, "text": segment.text}
                for segment in segments
            ], []

        repo.revise_segments_in_range = persist
        model = MagicMock()
        model.transcribe.return_value = ([
            FakeWhisperSeg(1.0, 2.0, "context only"),
            FakeWhisperSeg(6.0, 7.0, "mutable text"),
        ], SimpleNamespace())
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=model,
            cleanup=lambda: None,
        )
        engine = _make_engine(repo, backend)

        outcome = engine.revise_window("mic", frontier_s=100.0)

        audio = model.transcribe.call_args.args[0]
        assert audio.size == pytest.approx(50.0 * 16000, rel=0.01)
        assert outcome is not None
        assert [segment.text for segment in persisted["segments"]] == [
            "mutable text"
        ]
        assert persisted["segments"][0].start_s == pytest.approx(56.0)
