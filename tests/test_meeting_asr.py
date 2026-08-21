"""
Tests for live ASR (MeetingAsrEngine: fake backend, retry ×3, timestamped
segments), post-meeting offline ASR (silence split, overlap drop), and the
bounded rolling revision helpers plus their persistence.
"""
import json
import wave
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from meeting.asr.engine import (
    DRAFT_PROMPT_WORDS,
    FAST_MODE_BACKLOG_CHUNKS,
    MAX_ATTEMPTS,
    REVISE_MIN_ADVANCE_S,
    MeetingAsrEngine,
)
from meeting.asr.offline import (
    drop_overlapped_prefix,
    offline_cut_ranges,
    offline_segment_id,
)
from meeting.asr.revise import (
    REVISION_WINDOW_S,
    align_revision_start,
    build_initial_prompt,
    interval_iou,
    match_segments,
    revision_window,
    select_chunks_for_window,
    stitch_window_audio,
)
from meeting.capture.spool import TARGET_RATE
from meeting.interfaces import SpooledChunk, TranscriptSegment
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
        engine = MeetingAsrEngine(
            "base", "m_test", repo, enable_revisions=True
        )
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

    def test_revise_starts_at_mutable_boundary_without_duplicating_context(
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
            FakeWhisperSeg(1.0, 2.0, "first mutable text"),
            FakeWhisperSeg(6.0, 7.0, "more mutable text"),
        ], SimpleNamespace())
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=model,
            cleanup=lambda: None,
        )
        engine = _make_engine(repo, backend)

        outcome = engine.revise_window("mic", frontier_s=100.0)

        audio = model.transcribe.call_args.args[0]
        assert audio.size == pytest.approx(45.0 * 16000, rel=0.01)
        assert model.transcribe.call_args.kwargs["condition_on_previous_text"] is True
        assert outcome is not None
        assert [segment.text for segment in persisted["segments"]] == [
            "first mutable text",
            "more mutable text",
        ]
        assert persisted["segments"][0].start_s == pytest.approx(56.0)

    def test_revise_expands_boundary_to_preserve_crossing_segment_prefix(
        self, tmp_path
    ):
        path = str(tmp_path / "long.wav")
        _write_wav(path, duration_s=100.0)
        crossing = {
            "id": "sg_crossing",
            "meeting_id": "m_test",
            "chunk_id": 1,
            "channel": "mic",
            "start_s": 40.0,
            "end_s": 70.0,
            "text": "the complete original segment",
            "speaker_pinned": False,
        }
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
            lambda _meeting_id, _channel, start_s, _end_s:
            [crossing] if start_s <= crossing["end_s"] else []
        )
        persisted = {}

        def persist(_meeting_id, _channel, start_s, _end_s,
                    segments, remove_ids):
            persisted["start_s"] = start_s
            persisted["segments"] = segments
            persisted["remove_ids"] = remove_ids
            return [{"id": segment.segment_id} for segment in segments], []

        repo.revise_segments_in_range = persist
        model = MagicMock()
        model.transcribe.return_value = ([
            FakeWhisperSeg(0.0, 30.0, "the complete revised segment"),
        ], SimpleNamespace())
        backend = SimpleNamespace(
            is_available=lambda: True,
            model=model,
            cleanup=lambda: None,
        )
        engine = _make_engine(repo, backend)

        outcome = engine.revise_window("mic", frontier_s=100.0)

        audio = model.transcribe.call_args.args[0]
        assert audio.size == pytest.approx(60.0 * 16000, rel=0.01)
        assert outcome is not None
        assert persisted["start_s"] == 40.0
        assert persisted["remove_ids"] == []
        assert persisted["segments"][0].segment_id == "sg_crossing"
        assert persisted["segments"][0].start_s == 40.0


# --------------------------------------------------------------------------
# Post-meeting offline ASR: silence split and overlap drop.
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Bounded rolling revision helpers and persistence.
# --------------------------------------------------------------------------


def test_revision_window_caps_at_horizon():
    start, end = revision_window(100.0, window_s=45.0)
    assert start == 55.0
    assert end == 100.0
    start0, end0 = revision_window(10.0, window_s=45.0)
    assert start0 == 0.0
    assert end0 == 10.0


def test_align_revision_start_never_bisects_existing_segment():
    existing = [
        {"start_s": 40.0, "end_s": 70.0},
        {"start_s": 58.0, "end_s": 62.0},
    ]

    assert align_revision_start(55.0, existing) == 40.0
    assert align_revision_start(75.0, existing) == 75.0


def test_interval_iou_and_match_prefers_overlap():
    assert interval_iou(0, 10, 5, 15) == pytest.approx(5 / 15)
    existing = [
        {
            "id": "sg_a",
            "start_s": 0.0,
            "end_s": 4.0,
            "speaker_pinned": False,
            "speaker_participant_id": "p_me",
            "speaker_source": "channel",
            "chunk_id": 1,
            "channel": "mic",
            "meeting_id": "m1",
            "text": "helo",
        },
        {
            "id": "sg_b",
            "start_s": 4.0,
            "end_s": 8.0,
            "speaker_pinned": True,
            "speaker_participant_id": "p_me",
            "speaker_source": "human",
            "chunk_id": 1,
            "channel": "mic",
            "meeting_id": "m1",
            "text": "world",
        },
    ]
    decoded = [
        TranscriptSegment(
            segment_id="sg_new1",
            meeting_id="m1",
            chunk_id=2,
            channel="mic",
            start_s=0.2,
            end_s=3.8,
            text="hello",
        ),
        TranscriptSegment(
            segment_id="sg_new2",
            meeting_id="m1",
            chunk_id=2,
            channel="mic",
            start_s=8.5,
            end_s=11.0,
            text="again",
        ),
    ]
    plan = match_segments(existing, decoded)
    by_id = {seg.segment_id: seg for seg in plan.upserts}
    assert "sg_a" in by_id
    assert by_id["sg_a"].text == "hello"
    assert by_id["sg_a"].speaker_participant_id == "p_me"
    # Pinned unmatched old is kept (not deleted).
    assert "sg_b" not in plan.remove_ids
    # New unmatched insert kept with its new id.
    assert any(seg.segment_id == "sg_new2" for seg in plan.upserts)


def test_select_chunks_and_stitch(tmp_path):
    def write_wav(name, duration_s, amp=1000):
        path = tmp_path / name
        frames = np.full(int(duration_s * 16000), amp, dtype=np.int16)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(frames.tobytes())
        return str(path)

    chunks = [
        {
            "id": 1,
            "channel": "mic",
            "seq": 0,
            "start_s": 0.0,
            "duration_s": 5.0,
            "file_path": write_wav("a.wav", 5.0),
            "asr_status": "done",
        },
        {
            "id": 2,
            "channel": "mic",
            "seq": 1,
            "start_s": 5.0,
            "duration_s": 5.0,
            "file_path": write_wav("b.wav", 5.0),
            "asr_status": "done",
        },
        {
            "id": 3,
            "channel": "loopback",
            "seq": 0,
            "start_s": 0.0,
            "duration_s": 5.0,
            "file_path": write_wav("c.wav", 5.0),
            "asr_status": "done",
        },
    ]
    selected = select_chunks_for_window(chunks, "mic", 3.0, 8.0)
    assert [c["id"] for c in selected] == [1, 2]
    audio, start = stitch_window_audio(selected, 3.0, 8.0)
    assert start == pytest.approx(3.0)
    assert audio.size == pytest.approx(5.0 * 16000, rel=0.01)


def test_build_initial_prompt_truncates():
    segs = [{"text": "alpha"}, {"text": "beta " * 80}]
    prompt = build_initial_prompt(segs)
    assert len(prompt) <= 224
    assert "beta" in prompt


def test_revise_segments_in_range_persists(tmp_path, repo):
    meeting_id = "m_revise"
    repo.create_meeting(
        id=meeting_id,
        title="t",
        status="active",
        started_at=datetime.now().isoformat(),
        host_token="h",
        guest_token="g",
        cloud_enabled=False,
        spool_dir=str(tmp_path / "spool"),
        asr_model="base",
    )
    chunk_id = repo.register_chunk(
        meeting_id=meeting_id,
        channel="mic",
        seq=0,
        file_path=str(tmp_path / "x.wav"),
        start_s=0.0,
        duration_s=10.0,
        sample_rate=16000,
    )
    repo.commit_chunk_transcription(
        meeting_id,
        chunk_id,
        [
            TranscriptSegment(
                segment_id="sg_old",
                meeting_id=meeting_id,
                chunk_id=chunk_id,
                channel="mic",
                start_s=1.0,
                end_s=3.0,
                text="helo",
            )
        ],
    )
    upserts = [
        TranscriptSegment(
            segment_id="sg_old",
            meeting_id=meeting_id,
            chunk_id=chunk_id,
            channel="mic",
            start_s=1.0,
            end_s=3.2,
            text="hello",
        ),
        TranscriptSegment(
            segment_id="sg_new",
            meeting_id=meeting_id,
            chunk_id=chunk_id,
            channel="mic",
            start_s=4.0,
            end_s=6.0,
            text="there",
        ),
    ]
    rows, removed = repo.revise_segments_in_range(
        meeting_id, "mic", 0.0, 10.0, upserts, remove_ids=[]
    )
    assert removed == []
    texts = {r["id"]: r["text"] for r in rows}
    assert texts["sg_old"] == "hello"
    assert texts["sg_new"] == "there"
    assert REVISION_WINDOW_S == 45.0


def test_revise_keeps_segments_referenced_by_dashboard_evidence(tmp_path, repo):
    meeting_id = "m_evidence"
    repo.create_meeting(
        id=meeting_id,
        title="t",
        status="active",
        started_at=datetime.now().isoformat(),
        host_token="h",
        guest_token="g",
        cloud_enabled=True,
        spool_dir=str(tmp_path / "spool"),
        asr_model="base",
        state_json=json.dumps({
            "rolling_summary_evidence": ["sg_referenced"],
        }),
    )
    chunk_id = repo.register_chunk(
        meeting_id=meeting_id,
        channel="mic",
        seq=0,
        file_path=str(tmp_path / "x.wav"),
        start_s=0.0,
        duration_s=10.0,
        sample_rate=16000,
    )
    repo.commit_chunk_transcription(
        meeting_id,
        chunk_id,
        [TranscriptSegment(
            segment_id="sg_referenced",
            meeting_id=meeting_id,
            chunk_id=chunk_id,
            channel="mic",
            start_s=1.0,
            end_s=3.0,
            text="original evidence",
        )],
    )

    _rows, removed = repo.revise_segments_in_range(
        meeting_id,
        "mic",
        0.0,
        10.0,
        segments=[],
        remove_ids=["sg_referenced"],
    )

    assert removed == []
    assert repo.get_segment(meeting_id, "sg_referenced") is not None
