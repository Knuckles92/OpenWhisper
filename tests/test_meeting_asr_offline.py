"""Tests for the post-meeting offline ASR silence split and overlap drop."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.asr.offline import (
    drop_overlapped_prefix,
    offline_cut_ranges,
    offline_segment_id,
)
from meeting.capture.spool import TARGET_RATE
from meeting.interfaces import TranscriptSegment


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
