"""
Tests for the Meeting Mode spool: gap-fill (>120ms), quiet/hard cuts, and the
``SpoolWriter`` timeline (drift, overlap trimming, pause, atomic sequence).
"""
import os
import wave

import numpy as np
import pytest

from meeting.capture.spool import (
    DEFAULT_MAX_SEC,
    DEFAULT_TARGET_SEC,
    GAP_TOLERANCE_S,
    QUIET_WINDOW_S,
    TARGET_RATE,
    SpoolWriter,
    find_cut_point,
    gap_fill_frames,
)
from meeting.clock import MeetingClock
from meeting.interfaces import CaptureBlock

class TestGapFill:
    def test_no_fill_when_expected_is_none(self):
        fill = gap_fill_frames(None, 1.0)
        assert fill.size == 0
        assert fill.dtype == np.int16

    def test_no_fill_within_tolerance(self):
        # Gaps at or below 120ms are absorbed as timing jitter.
        assert gap_fill_frames(0.0, GAP_TOLERANCE_S).size == 0
        assert gap_fill_frames(0.0, 0.10, sample_rate=TARGET_RATE).size == 0
        assert gap_fill_frames(2.5, 2.5 + 0.05).size == 0

    def test_fills_gap_above_120ms(self):
        expected = 5.0
        actual = 5.0 + 0.25  # 250ms > 120ms
        fill = gap_fill_frames(expected, actual, sample_rate=TARGET_RATE)
        assert fill.dtype == np.int16
        assert np.all(fill == 0)
        assert fill.size == int(round(0.25 * TARGET_RATE))

    def test_gap_just_over_tolerance(self):
        # Use a clearly-over gap to avoid float edge at exactly 0.12
        gap_s = 0.121
        fill = gap_fill_frames(0.0, gap_s)
        assert fill.size == int(round(gap_s * TARGET_RATE))
        assert fill.size > 0

class TestFindCutPoint:
    def _quiet_buffer(self, duration_s, quiet_from_s=None, loud_amp=5000):
        """Synthetic int16 buffer: loud then optional quiet tail."""
        n = int(duration_s * TARGET_RATE)
        buf = np.full(n, loud_amp, dtype=np.int16)
        if quiet_from_s is not None:
            q0 = int(quiet_from_s * TARGET_RATE)
            buf[q0:] = 0
        return buf

    def test_no_cut_before_target(self):
        buf = self._quiet_buffer(4.9, quiet_from_s=3.0)
        assert find_cut_point(
            buf, TARGET_RATE,
            target_sec=DEFAULT_TARGET_SEC,
            max_sec=DEFAULT_MAX_SEC,
        ) is None

    def test_quiet_cut_after_target_at_400ms(self):
        # Loud just past the live target, then silence: cut at quiet-window end.
        quiet_from_s = DEFAULT_TARGET_SEC + 0.1
        buf = self._quiet_buffer(7.0, quiet_from_s=quiet_from_s)
        cut = find_cut_point(
            buf, TARGET_RATE,
            target_sec=DEFAULT_TARGET_SEC,
            max_sec=DEFAULT_MAX_SEC,
        )
        assert cut is not None
        # Cut lands at start_of_quiet + quiet_window
        expected = int(quiet_from_s * TARGET_RATE) + int(
            QUIET_WINDOW_S * TARGET_RATE
        )
        assert abs(cut - expected) <= int(0.05 * TARGET_RATE) + 1
        assert cut / TARGET_RATE >= (
            DEFAULT_TARGET_SEC + QUIET_WINDOW_S - 0.15
        )

    def test_hard_cut_at_max_without_quiet(self):
        buf = self._quiet_buffer(DEFAULT_MAX_SEC + 1.0)  # all loud
        cut = find_cut_point(
            buf, TARGET_RATE,
            target_sec=DEFAULT_TARGET_SEC,
            max_sec=DEFAULT_MAX_SEC,
        )
        assert cut == int(DEFAULT_MAX_SEC * TARGET_RATE)

    def test_no_hard_cut_until_max(self):
        # Between target and max with no quiet: wait for more audio
        buf = self._quiet_buffer((DEFAULT_TARGET_SEC + DEFAULT_MAX_SEC) / 2.0)
        assert find_cut_point(
            buf, TARGET_RATE,
            target_sec=DEFAULT_TARGET_SEC,
            max_sec=DEFAULT_MAX_SEC,
        ) is None

    def test_native_rate_cut_matches_16k_cut(self):
        # The writer scans at the device's native rate; the policy must not
        # shift when the same audio is presented at 44.1 kHz.
        quiet_from_s = DEFAULT_TARGET_SEC + 0.1
        n = int(7.0 * 44100)
        buf = np.full(n, 5000, dtype=np.int16)
        buf[int(quiet_from_s * 44100):] = 0
        cut = find_cut_point(
            buf, 44100,
            target_sec=DEFAULT_TARGET_SEC,
            max_sec=DEFAULT_MAX_SEC,
        )
        assert cut is not None
        assert abs(cut / 44100 - (quiet_from_s + QUIET_WINDOW_S)) <= 0.06

# SpoolWriter

class FakeRepo:
    """Dict-backed stand-in for ``MeetingRepository.register_chunk``."""

    def __init__(self, fail_times=0):
        self.chunks = {}
        self.events = []          # ordered ("register"|"on_chunk", chunk_id)
        self.fail_times = fail_times
        self._next_id = 1

    def register_chunk(self, **fields):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("database is locked")
        chunk_id = self._next_id
        self._next_id += 1
        self.chunks[chunk_id] = dict(fields)
        self.events.append(("register", chunk_id, fields["asr_status"]))
        return chunk_id

class Collector:
    """``on_chunk`` sink that shares the repository's event log."""

    def __init__(self, repo):
        self.repo = repo
        self.chunks = []

    def __call__(self, chunk):
        self.chunks.append(chunk)
        self.repo.events.append(("on_chunk", chunk.chunk_id, chunk.start_s))

BLOCK = 1024

def _make_writer(tmp_path, repo, collector, clock, **kwargs):
    """SpoolWriter with a queue big enough that a test never drops blocks."""
    return SpoolWriter(
        "m_test", "mic", str(tmp_path), clock, repo, on_chunk=collector,
        queue_size=100000, **kwargs
    )

def _signal(duration_s, rate, quiet_every_s=None, quiet_len_s=0.6, amp=6000):
    """Loud int16 tone-ish signal with periodic silent stretches."""
    n = int(duration_s * rate)
    t = np.arange(n, dtype=np.float64)
    sig = (amp * np.sin(2 * np.pi * 440.0 * t / rate)).astype(np.int16)
    if quiet_every_s:
        step = int(quiet_every_s * rate)
        span = int(quiet_len_s * rate)
        for start in range(step, n, step):
            sig[start:start + span] = 0
    return sig

def _feed_stream(writer, signal, rate, t0, start_index=0, stall_s=0.0,
                 burst_blocks=0):
    """Feed ``signal`` in 1024-frame blocks with real-time timestamps.

    Args:
        writer: The ``SpoolWriter`` under test.
        signal: 1-D int16 samples to deliver.
        rate: Native sample rate of ``signal``.
        t0: Monotonic instant the stream's first sample was captured at.
        start_index: Sample offset within the meeting the stream resumes at.
        stall_s: Real-time stall inserted before ``burst_blocks`` blocks,
            which are then all stamped at the stall's end (how ``soundcard``
            behaves: it buffers instead of dropping and stamps at delivery).
        burst_blocks: Number of blocks delivered in that burst.

    Returns:
        The sample index just past the last block fed.
    """
    idx = 0
    burst_at = None
    if burst_blocks:
        burst_at = int(stall_s * rate)  # burst covers the stalled span
    while idx + BLOCK <= signal.size:
        block = signal[idx:idx + BLOCK]
        abs_index = start_index + idx
        if burst_at is not None and abs_index >= burst_at:
            # Everything in the burst is stamped at the same delivery instant.
            t_mono = t0 + burst_at / float(rate)
            burst_blocks -= 1
            if burst_blocks <= 0:
                burst_at = None
        else:
            t_mono = t0 + abs_index / float(rate)
        writer.feed(CaptureBlock(channel="mic", frames=block,
                                 sample_rate=rate, t_mono=t_mono))
        idx += BLOCK
    return idx

def _start_clock():
    """A started ``MeetingClock`` plus the monotonic instant it started at."""
    import time
    clock = MeetingClock()
    clock.start()
    return clock, time.monotonic()

class TestSpoolWriterTimeline:
    def test_no_drift_over_60s_at_44100(self, tmp_path):
        """Chunk offsets must track real time, not resampled sample counts.

        Per-block ``round(n * 16000 / 44100)`` emits 372 samples where
        371.5193 is exact, so the old implementation ran ~0.9 ms/s fast:
        +54 ms after a minute, +4.7 s after an hour. Both assertions below
        fail against it.
        """
        rate = 44100
        duration = 60.0
        repo = FakeRepo()
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)

        signal = _signal(duration, rate, quiet_every_s=7.0)
        fed = _feed_stream(writer, signal, rate, t0)
        writer.flush()

        chunks = collector.chunks
        assert len(chunks) >= 3, "expected several quiet-point cuts in 60s"
        # Stream starts at meeting time zero.
        assert chunks[0].start_s == pytest.approx(0.0, abs=0.02)
        # Consecutive chunks are contiguous.
        for prev, nxt in zip(chunks, chunks[1:]):
            assert nxt.start_s == pytest.approx(
                prev.start_s + prev.duration_s, abs=0.005
            )
        # And the whole timeline matches the real elapsed audio.
        fed_s = fed / float(rate)
        end_s = chunks[-1].start_s + chunks[-1].duration_s
        assert end_s == pytest.approx(fed_s, abs=0.02)

    def test_negative_gap_does_not_shift_the_channel(self, tmp_path):
        """A stall + burst must not push the channel permanently forward."""
        rate = 48000
        repo = FakeRepo()
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)

        # 5s clean, then a 0.4s stall whose buffered audio is delivered as a
        # burst of 20 same-timestamp blocks, then 25s clean.
        signal = _signal(30.0, rate, quiet_every_s=7.0)
        stall_s = 5.0
        _feed_stream(writer, signal, rate, t0, stall_s=stall_s, burst_blocks=20)
        writer.flush()

        chunks = collector.chunks
        assert chunks, "expected at least one chunk"
        end_s = chunks[-1].start_s + chunks[-1].duration_s
        fed_s = (signal.size // BLOCK) * BLOCK / float(rate)
        # Without overlap trimming the burst adds ~0.43s that never comes
        # back; the tolerance here is one GAP_TOLERANCE_S of slack.
        assert end_s == pytest.approx(fed_s, abs=GAP_TOLERANCE_S + 0.03)

    def test_positive_gap_is_zero_filled(self, tmp_path):
        """A >120ms silence is filled so the timeline stays aligned."""
        rate = 16000
        repo = FakeRepo()
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)

        chunk_a = _signal(1.0, rate)
        chunk_b = _signal(1.0, rate)
        _feed_stream(writer, chunk_a, rate, t0)
        # Resume 0.5s later than the stream ended: a real dropout.
        resume_index = int(1.5 * rate)
        _feed_stream(writer, chunk_b, rate, t0, start_index=resume_index)
        writer.flush()

        assert len(collector.chunks) == 1
        chunk = collector.chunks[0]
        assert chunk.start_s == pytest.approx(0.0, abs=0.02)
        with wave.open(chunk.file_path, "rb") as wav:
            data = np.frombuffer(wav.readframes(wav.getnframes()),
                                 dtype=np.int16)
        # 1s audio + 0.5s fill + 1s audio (block-quantized).
        assert chunk.duration_s == pytest.approx(2.5, abs=0.08)
        assert data.size == int(round(chunk.duration_s * TARGET_RATE))
        mid = data[int(1.05 * rate):int(1.45 * rate)]
        assert np.all(mid == 0), "the dropout must be silence, not stretched audio"
        assert np.abs(data[int(1.6 * rate):int(2.4 * rate)]).max() > 1000

    def test_blocks_fed_while_paused_are_dropped(self, tmp_path):
        rate = 16000
        repo = FakeRepo()
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)

        clock.pause()
        _feed_stream(writer, _signal(3.0, rate), rate, t0)
        writer.flush()

        assert collector.chunks == []
        assert repo.chunks == {}
        assert os.listdir(tmp_path) == []

class TestSpoolWriterContract:
    def test_registered_pending_before_on_chunk(self, tmp_path):
        rate = 16000
        repo = FakeRepo()
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)

        _feed_stream(writer, _signal(2.0, rate), rate, t0)
        writer.flush()

        assert len(collector.chunks) == 1
        chunk = collector.chunks[0]
        kinds = [event[0] for event in repo.events]
        assert kinds == ["register", "on_chunk"]
        assert repo.events[0][2] == "pending"
        assert repo.chunks[chunk.chunk_id]["sample_rate"] == TARGET_RATE
        assert repo.chunks[chunk.chunk_id]["seq"] == 0
        # Atomic write: the temp file never survives.
        assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []
        assert os.path.basename(chunk.file_path) == "mic_00000.wav"
        with wave.open(chunk.file_path, "rb") as wav:
            assert wav.getframerate() == TARGET_RATE
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2

    def test_short_remainder_is_kept(self, tmp_path):
        # 0.5s of final speech must survive the flush (MIN_FLUSH_S = 0.25).
        rate = 16000
        repo = FakeRepo()
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)

        _feed_stream(writer, _signal(0.5, rate), rate, t0)
        result = writer.flush()

        assert result is not None
        assert len(collector.chunks) == 1
        assert collector.chunks[0].duration_s == pytest.approx(0.46, abs=0.05)

    def test_flush_is_idempotent(self, tmp_path):
        rate = 16000
        repo = FakeRepo()
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)

        _feed_stream(writer, _signal(1.0, rate), rate, t0)
        first = writer.flush()
        second = writer.flush()

        assert first is second
        assert len(collector.chunks) == 1
        # Feeding after flush is harmless.
        writer.feed(CaptureBlock(channel="mic", frames=_signal(0.1, rate),
                                 sample_rate=rate, t_mono=t0 + 5.0))
        assert len(collector.chunks) == 1

    def test_registration_failure_orphans_the_wav(self, tmp_path):
        rate = 16000
        repo = FakeRepo(fail_times=2)  # both the attempt and its retry fail
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)

        _feed_stream(writer, _signal(1.0, rate), rate, t0)
        writer.flush()

        assert collector.chunks == []
        names = os.listdir(tmp_path)
        assert "mic_00000.wav.orphan" in names
        assert collector.chunks == []
        session_names = {
            name for name in names
            if name.startswith("mic_session") or name.endswith(".16k.pcm")
        }
        assert session_names, names

class TestSessionWav:
    def test_flush_writes_16k_session_wav(self, tmp_path):
        rate = 16000
        repo = FakeRepo()
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)
        _feed_stream(writer, _signal(2.0, rate), rate, t0)
        writer.flush()

        wav_path = os.path.join(tmp_path, "mic_session.wav")
        meta_path = os.path.join(tmp_path, "mic_session.json")
        assert os.path.isfile(wav_path)
        assert os.path.isfile(meta_path)
        with wave.open(wav_path, "rb") as wav:
            assert wav.getframerate() == TARGET_RATE
            assert wav.getnchannels() == 1
            nframes = wav.getnframes()
        assert nframes == pytest.approx(int(2.0 * TARGET_RATE), abs=BLOCK)

    def test_pause_does_not_create_session_files(self, tmp_path):
        rate = 16000
        repo = FakeRepo()
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)
        clock.pause()
        _feed_stream(writer, _signal(3.0, rate), rate, t0)
        writer.flush()
        assert os.listdir(tmp_path) == []

    def test_native_rate_session_matches_timeline(self, tmp_path):
        rate = 44100
        repo = FakeRepo()
        collector = Collector(repo)
        clock, t0 = _start_clock()
        writer = _make_writer(tmp_path, repo, collector, clock)
        _feed_stream(writer, _signal(3.0, rate), rate, t0)
        writer.flush()
        wav_path = os.path.join(tmp_path, "mic_session.wav")
        with wave.open(wav_path, "rb") as wav:
            duration = wav.getnframes() / float(wav.getframerate())
        fed_s = (int(3.0 * rate) // BLOCK) * BLOCK / float(rate)
        assert duration == pytest.approx(fed_s, abs=0.05)

    def test_chunk_concat_fallback(self, tmp_path):
        from meeting.capture.spool import concat_channel_chunks_to_wav

        rate = TARGET_RATE
        chunk_a = tmp_path / "loopback_00000.wav"
        chunk_b = tmp_path / "loopback_00001.wav"
        for path, samples in (
            (chunk_a, _signal(0.5, rate)),
            (chunk_b, _signal(0.5, rate)),
        ):
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(samples.tobytes())
        out = tmp_path / "loopback_session.wav"
        origin = concat_channel_chunks_to_wav(
            [
                {
                    "channel": "loopback", "seq": 0, "file_path": str(chunk_a),
                    "start_s": 0.0, "duration_s": 0.5,
                },
                {
                    "channel": "loopback", "seq": 1, "file_path": str(chunk_b),
                    "start_s": 0.5, "duration_s": 0.5,
                },
            ],
            "loopback",
            str(out),
        )
        assert origin == pytest.approx(0.0)
        with wave.open(str(out), "rb") as wav:
            assert wav.getnframes() == pytest.approx(rate, abs=2)
