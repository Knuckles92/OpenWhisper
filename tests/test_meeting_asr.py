"""
Tests for live ASR (MeetingAsrEngine: fake backend, retry ×3, timestamped
segments) and post-meeting offline ASR (silence split, overlap drop).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from meeting.asr.engine import (
    DRAFT_PROMPT_WORDS,
    FAST_MODE_BACKLOG_CHUNKS,
    MAX_ATTEMPTS,
    MeetingAsrEngine,
)
from meeting.asr.offline import (
    OfflineWindowError,
    drop_overlapped_prefix,
    offline_cut_ranges,
    offline_segment_id,
    transcribe_session_audio,
)
from meeting.capture.spool import TARGET_RATE
from meeting.interfaces import SpooledChunk, TranscriptSegment
from meeting.persist.repository import interval_iou
from tests.helpers import write_wav as _write_wav


class FakeRepository:
    def __init__(self):
        self.statuses = []
        self.pending = []

    def set_chunk_status(self, chunk_id, status, error=None):
        self.statuses.append((chunk_id, status, error))

    def get_pending_chunks(self, meeting_id):
        return list(self.pending)


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
            assert kwargs["language"] is None
            assert kwargs["initial_prompt"] is None
        finally:
            engine.stop()

    def test_next_chunk_receives_bounded_committed_context(self, tmp_path):
        repo = FakeRepository()
        model = MagicMock()
        model.transcribe.side_effect = [
            ([FakeWhisperSeg(0.0, 0.4, "one two three")], SimpleNamespace()),
            ([FakeWhisperSeg(0.0, 0.4, "four")], SimpleNamespace()),
        ]
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=model,
            cleanup=lambda: None,
        )
        engine = _make_engine(repo, backend)
        engine.start(on_chunk_result=lambda chunk, segments: None)
        try:
            first = _chunk(tmp_path, chunk_id=1, start_s=0.0)
            second = _chunk(tmp_path, chunk_id=2, start_s=1.0)
            engine.enqueue(first)
            assert engine.drain(5.0)
            engine.enqueue(second)
            assert engine.drain(5.0)

            calls = model.transcribe.call_args_list
            assert calls[0].kwargs["initial_prompt"] is None
            assert calls[1].kwargs["initial_prompt"] == "one two three"
            assert len(engine._draft_context[("m_test", "mic")]) <= DRAFT_PROMPT_WORDS
        finally:
            engine.stop()

    def test_recovery_context_excludes_later_and_other_channel_rows(self, tmp_path):
        repo = FakeRepository()
        repo.get_segments = lambda _meeting_id, after_start_s=-1.0: [
            {"channel": "mic", "end_s": 4.0, "text": "safe earlier words"},
            {"channel": "loopback", "end_s": 4.0, "text": "other channel"},
            {"channel": "mic", "end_s": 8.0, "text": "future words"},
        ]
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=MagicMock(),
            cleanup=lambda: None,
        )
        engine = _make_engine(repo, backend)

        prompt = engine._draft_prompt(_chunk(tmp_path, start_s=5.0))

        assert prompt == "safe earlier words"

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
        _write_wav(chunk.file_path, value=0)
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


class TestOfflineLanguage:
    def _engine(self):
        backend = SimpleNamespace(
            is_available=lambda: True, model=MagicMock(), cleanup=lambda: None,
        )
        return _make_engine(FakeRepository(), backend)

    @staticmethod
    def _vote(engine, language, probability=0.95, held_speech=True, times=1):
        info = SimpleNamespace(language=language, language_probability=probability)
        for _ in range(times):
            engine._record_language(info, held_speech)

    def test_clear_single_language_carries_into_the_offline_pass(self):
        engine = self._engine()
        self._vote(engine, "de", times=9)
        self._vote(engine, "en")
        assert engine.dominant_language() == "de"

    def test_bilingual_or_thin_evidence_keeps_auto(self):
        mixed = self._engine()
        self._vote(mixed, "de", times=6)
        self._vote(mixed, "en", times=3)
        assert mixed.dominant_language() is None

        thin = self._engine()
        self._vote(thin, "de", times=4)
        self._vote(thin, "de", probability=0.5, times=5)
        self._vote(thin, "de", held_speech=False, times=5)
        assert thin.dominant_language() is None

    def test_configured_language_always_wins(self):
        engine = self._engine()
        engine.language = "fr"
        self._vote(engine, "de", times=9)
        assert engine.dominant_language() == "fr"
        assert engine._language_votes == {}


# Post-meeting offline ASR: silence split and overlap drop.


class TestOfflineCutRanges:
    def test_hard_cut_without_audio(self):
        total = int(90 * TARGET_RATE)
        ranges = offline_cut_ranges(total, TARGET_RATE, audio=None, overlap_s=1.0)
        assert ranges
        assert ranges[0][0] == 0
        assert ranges[-1][1] == total
        # Consecutive windows overlap by about 1s.
        for prev, nxt in zip(ranges, ranges[1:]):
            overlap = prev[1] - nxt[0]
            assert overlap == pytest.approx(TARGET_RATE, abs=2)

    def test_quiet_gap_cuts_before_max(self):
        n = int(40 * TARGET_RATE)
        audio = np.full(n, 5000, dtype=np.int16)
        quiet_from = int(16 * TARGET_RATE)
        audio[quiet_from:quiet_from + int(1.0 * TARGET_RATE)] = 0
        ranges = offline_cut_ranges(
            n, TARGET_RATE, audio, target_sec=15.0, max_sec=25.0,
            quiet_window_s=0.7, overlap_s=1.0,
        )
        first_end = ranges[0][1] / TARGET_RATE
        assert first_end < 25.0
        assert first_end == pytest.approx(16.7, abs=0.15)


class TestOverlapDrop:
    def test_keeps_later_window_tail(self):
        segs = [
            TranscriptSegment(
                segment_id="a", meeting_id="m", chunk_id=None, channel="mic",
                start_s=9.2, end_s=9.8, text="only overlap",
            ),
            TranscriptSegment(
                segment_id="b", meeting_id="m", chunk_id=None, channel="mic",
                start_s=10.5, end_s=12.0, text="new",
            ),
        ]
        kept = drop_overlapped_prefix(segs, keep_from_s=10.0)
        assert [seg.text for seg in kept] == ["new"]

    def test_keeps_segment_that_starts_in_overlap_but_extends_past(self):
        segs = [
            TranscriptSegment(
                segment_id="a", meeting_id="m", chunk_id=None, channel="mic",
                start_s=9.2, end_s=9.8, text="only overlap",
            ),
            TranscriptSegment(
                segment_id="b", meeting_id="m", chunk_id=None, channel="mic",
                start_s=9.5, end_s=12.0, text="spans boundary",
            ),
        ]
        kept = drop_overlapped_prefix(segs, keep_from_s=10.0)
        assert [seg.text for seg in kept] == ["spans boundary"]

    def test_offline_ids_are_stable(self):
        first = offline_segment_id("m1", "mic", 1.25, 0)
        second = offline_segment_id("m1", "mic", 1.25, 0)
        other = offline_segment_id("m1", "mic", 1.25, 1)
        assert first == second
        assert first.startswith("sg_")
        assert first != other

    def test_failed_window_aborts_instead_of_returning_partial_transcript(self):
        model = MagicMock()
        model.transcribe.side_effect = [
            ([FakeWhisperSeg(0.0, 0.5, "first window")], SimpleNamespace()),
            RuntimeError("decoder failed"),
        ]
        frames = np.full(2 * TARGET_RATE, 1000, dtype=np.int16)

        with pytest.raises(OfflineWindowError, match=r"window 2/2"):
            transcribe_session_audio(
                model,
                frames,
                TARGET_RATE,
                meeting_id="m1",
                channel="mic",
                target_sec=1.0,
                max_sec=1.0,
            )


def test_interval_iou():
    assert interval_iou(0, 10, 5, 15) == pytest.approx(5 / 15)
    assert interval_iou(0, 5, 5, 10) == 0.0
    assert interval_iou(2, 2, 2, 2) == 0.0


def test_offline_decoder_internal_typeerror_is_not_retried(tmp_path, monkeypatch):
    from meeting.asr.offline import transcribe_meeting_sessions

    monkeypatch.setattr(
        "meeting.asr.offline.load_channel_session",
        lambda *args: (np.ones(16000, dtype=np.int16), 16000, 0.0),
    )
    calls = []

    def decoder(*args, **kwargs):
        calls.append(kwargs)
        raise TypeError("decoder internal failure")

    with pytest.raises(TypeError, match="decoder internal failure"):
        transcribe_meeting_sessions(
            object(), str(tmp_path), "m_test", channels=("mic",),
            transcribe_fn=decoder,
        )
    assert len(calls) == 1
    assert "progress_cb" in calls[0]
