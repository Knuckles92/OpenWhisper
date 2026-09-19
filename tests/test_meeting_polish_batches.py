"""Bound cleanup work without losing speech or changing segment identity."""
import random

import pytest

from meeting.finalization import (
    POLISH_MAX_SEGMENTS,
    POLISH_MAX_TEXT_CHARS,
    polish_blocks,
)


def assert_complete_bounded_coverage(rows, blocks):
    seen = {}
    for block in blocks:
        assert 0 < len(block) <= POLISH_MAX_SEGMENTS
        assert len(block) == 1 or sum(len(row["text"]) for row in block) <= POLISH_MAX_TEXT_CHARS
        ids = [row["id"] for row in block]
        assert any(segment_id not in seen for segment_id in ids)
        positions = [rows.index(row) for row in block]
        assert positions == list(range(positions[0], positions[0] + len(block)))
        seen.update((row["id"], row) for row in block)
    assert list(seen.values()) == rows


@pytest.mark.parametrize("count", [0, 1, 79, 80, 81, 144, 152, 160, 435])
def test_short_segments_have_complete_coverage_without_context_only_tail(count):
    rows = [{"id": f"sg_{i}", "text": "short line"} for i in range(count)]
    blocks = polish_blocks(rows)
    assert_complete_bounded_coverage(rows, blocks)
    if count == 435:
        assert len(blocks) == 6
        assert max(map(len, blocks)) == 80
        assert blocks[0][-8:] == blocks[1][:8]
    if count == 152:
        assert len(blocks) == 2


def test_long_segments_use_text_budget_even_below_segment_limit():
    rows = [{"id": f"sg_{i}", "text": "x" * 2_000} for i in range(12)]
    blocks = polish_blocks(rows)
    assert len(blocks) > 1
    assert_complete_bounded_coverage(rows, blocks)


def test_oversized_segment_is_preserved_and_does_not_block_following_speech():
    rows = [
        {"id": "sg_before", "text": "Before."},
        {"id": "sg_large", "text": "x" * (POLISH_MAX_TEXT_CHARS + 1)},
        {"id": "sg_after", "text": "After."},
    ]
    blocks = polish_blocks(rows)
    assert_complete_bounded_coverage(rows, blocks)
    assert blocks[1] == [rows[1]]
    assert blocks[1][0] is rows[1]


def test_mixed_lengths_always_advance_and_preserve_text():
    rng = random.Random(419)
    rows = [
        {"id": f"sg_{i}", "text": "x" * rng.choice([0, 30, 200, 2_000, 7_000, 9_000])}
        for i in range(435)
    ]
    assert_complete_bounded_coverage(rows, polish_blocks(rows))
