"""Pure tests for the multi-file upload request model and text helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import config
from services.batch_upload import (
    BatchItem,
    BatchRelation,
    BatchUploadRequest,
    batch_source_name,
    compose_batch_cleanup_prompt,
    format_batch_transcript,
    join_raw_parts,
)


def _request(relation, count=2, **kwargs):
    items = tuple(BatchItem(f"/audio/part{i}.mp3", 60.0) for i in range(count))
    return BatchUploadRequest(items=items, relation=relation, **kwargs)


class TestBatchUploadRequest:
    def test_sequential_combines_and_separate_does_not(self):
        assert _request(BatchRelation.SEQUENTIAL).combine is True
        assert _request(BatchRelation.SEPARATE).combine is False

    def test_custom_follows_its_own_checkbox(self):
        assert _request(BatchRelation.CUSTOM, custom_combine=True).combine is True
        assert _request(BatchRelation.CUSTOM, custom_combine=False).combine is False

    def test_separate_has_no_batch_context(self):
        """Separate must clean exactly like a single-file upload."""
        request = _request(BatchRelation.SEPARATE)
        assert request.batch_context() is None
        assert request.batch_context(request.items[0]) is None

    def test_sequential_context_names_the_parts_in_order(self):
        context = _request(BatchRelation.SEQUENTIAL, count=3).batch_context()
        assert "3 consecutive parts" in context
        assert "part0.mp3, part1.mp3, part2.mp3" in context

    def test_custom_combined_context_carries_the_user_text(self):
        request = _request(
            BatchRelation.CUSTOM,
            custom_instructions="  Two interviews with the same client.  ",
            custom_combine=True,
        )
        context = request.batch_context()
        assert context.startswith(
            config.TRANSCRIPT_BATCH_CUSTOM_COMBINED_NOTE.format(
                count=2, names="part0.mp3, part1.mp3"
            )
        )
        assert context.endswith("Two interviews with the same client.")

    def test_custom_per_file_context_names_the_current_file(self):
        request = _request(
            BatchRelation.CUSTOM, custom_instructions="Voice memos.", count=3
        )
        context = request.batch_context(request.items[1])
        assert "This transcript is part1.mp3, one of 3 recordings" in context
        assert context.endswith("Voice memos.")

    def test_custom_with_nothing_typed_has_no_context(self):
        request = _request(BatchRelation.CUSTOM, custom_instructions="   ")
        assert request.batch_context() is None
        assert request.batch_context(request.items[0]) is None


class TestComposeBatchCleanupPrompt:
    def test_base_prompt_comes_first_then_header_context_and_guard(self):
        prompt = compose_batch_cleanup_prompt("BASE\n\nRules: 1. x", "CONTEXT")
        assert prompt.startswith("BASE\n\nRules: 1. x\n\n")
        header_at = prompt.index(config.TRANSCRIPT_BATCH_CONTEXT_HEADER)
        context_at = prompt.index("CONTEXT")
        guard_at = prompt.index(config.TRANSCRIPT_BATCH_CONTEXT_GUARD)
        assert header_at < context_at < guard_at


class TestTextHelpers:
    def test_join_raw_parts_uses_blank_lines_and_drops_empties(self):
        assert join_raw_parts(["  one ", "", "   ", "two"]) == "one\n\ntwo"

    def test_join_raw_parts_of_nothing_is_empty(self):
        assert join_raw_parts([]) == ""
        assert join_raw_parts(["", "  "]) == ""

    def test_format_batch_transcript_heads_each_section_with_its_name(self):
        text = format_batch_transcript([("a.wav", " Hello. "), ("b.wav", "Bye.")])
        assert text == "## a.wav\n\nHello.\n\n## b.wav\n\nBye."

    def test_batch_source_name_lists_every_file_when_short(self):
        assert batch_source_name(["a.mp3", "b.mp3"]) == "2 files: a.mp3, b.mp3"

    def test_batch_source_name_drops_trailing_names_past_the_limit(self):
        names = [f"recording_{i:02d}.mp3" for i in range(20)]
        label = batch_source_name(names, limit=60)
        assert label.startswith("20 files: recording_00.mp3")
        assert label.endswith(" more")
        assert len(label) <= 60
        assert "recording_19.mp3" not in label

    def test_batch_source_name_always_keeps_the_first_file(self):
        label = batch_source_name(["a_very_long_recording_name.mp3", "b.mp3"], limit=10)
        assert label == "2 files: a_very_long_recording_name.mp3, +1 more"
