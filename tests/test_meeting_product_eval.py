"""Unit tests for the product-package eval helpers (no live LLM)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.meeting_mode.product_eval import (
    ProductEvalHost,
    dashboard_package,
    format_reference,
)
from meeting.interfaces import OpResult


def test_dashboard_package_drops_removed_cards_and_empty_transcript_lines():
    snapshot = {
        "title": "Demo",
        "topic": {"current": "Keyword spotting"},
        "rolling_summary": "They compared transforms.",
        "cards": {
            "key_points": [
                {"text": "Keep me", "status": "proposed", "data": {}},
                {"text": "Gone", "status": "removed", "data": {}},
            ],
            "decisions": [],
            "action_items": [],
            "risks": [],
            "timeline": [],
            "user_notes": [{"text": "secret", "status": "proposed"}],
        },
        "questions": [],
        "participants": {},
    }
    package = dashboard_package(
        snapshot,
        [
            {"id": "sg_1", "start_s": 1.0, "end_s": 2.0, "text": "hello"},
            {"id": "sg_2", "start_s": 3.0, "end_s": 4.0, "text": "  "},
        ],
    )
    assert package["topic"] == "Keyword spotting"
    assert package["cards"]["key_points"] == [{"text": "Keep me", "data": {}}]
    assert "user_notes" not in package["cards"]
    assert [row["id"] for row in package["transcript"]] == ["sg_1"]


def test_host_polish_rewrites_segment_text_in_place():
    host = ProductEvalHost(
        "m1",
        [{"id": "sg_1", "start_s": 0.0, "end_s": 1.0, "text": "drafft"}],
    )
    host.allow_agent_writes()
    results = host.apply_agent_ops([{
        "op": "revise_segment_text",
        "segment_id": "sg_1",
        "text": "draft",
        "evidence": ["sg_1"],
    }])
    assert results[0].ok
    assert host.get_transcript()[0]["text"] == "draft"


def test_host_rejects_ops_when_writes_are_revoked():
    host = ProductEvalHost(
        "m1",
        [{"id": "sg_1", "start_s": 0.0, "end_s": 1.0, "text": "hi"}],
    )
    results = host.apply_agent_ops([{
        "op": "set_topic", "text": "Nope", "evidence": ["sg_1"],
    }])
    assert results[0].ok is False
    assert results[0].reason == "agent_writes_revoked"


def test_format_reference_groups_speaker_turns():
    class Word:
        def __init__(self, text, speaker):
            self.text = text
            self.speaker = speaker
            self.start_s = 0.0
            self.end_s = 0.1

    text = format_reference([
        Word("hello", "A"),
        Word("there", "A"),
        Word("hi", "B"),
    ])
    assert "A: hello there" in text
    assert "B: hi" in text


def test_segment_handler_returns_inverse_for_undo():
    host = ProductEvalHost(
        "m1",
        [{"id": "sg_1", "start_s": 0.0, "end_s": 1.0, "text": "old"}],
    )
    inverse = host._on_segment_op(OpResult(
        ok=True,
        op={"op": "revise_segment_text"},
        effect={"segment_id": "sg_1", "text": "new"},
    ))
    assert inverse["text"] == "old"
    assert host._segments["sg_1"]["text"] == "new"
