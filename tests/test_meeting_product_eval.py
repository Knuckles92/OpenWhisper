"""Unit tests for the product-package eval helpers (no live LLM)."""

from benchmarks.meeting_mode.product_eval import (
    ProductEvalHost,
    _render_package_for_judge,
    build_live_windows,
    dashboard_package,
    format_reference,
    strip_proposed_after_redecode,
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

def test_render_package_for_judge_formats_notes_and_timeline():
    package = {
        "topic": "Transforms",
        "summary": "Comparing Fourier and Wavelet transforms.",
        "cards": {
            "key_points": [{"text": "Point 1", "data": {}}],
            "decisions": [],
            "action_items": [
                {"text": "Ship feature", "data": {"owner_participant_id": "p_alice"}},
            ],
            "risks": [
                {"text": "High latency", "data": {"severity": "high"}},
            ],
            "timeline": [
                {"text": "Opening discussion", "data": {"start_s": 12.5}},
            ],
            "live_notes": [
                {
                    "text": "The speaker discussed FFT performance.",
                    "data": {"heading": "FFT Analysis", "start_s": 45.0},
                },
            ],
        },
        "questions": [
            {"text": "What about memory?", "status": "resolved", "answer_text": "2GB"},
        ],
        "transcript": [
            {"id": "sg_1", "start_s": 0.0, "end_s": 2.0, "text": "Hello world"},
        ],
    }
    rendered = _render_package_for_judge(package)
    assert "Topic: Transforms" in rendered
    assert "- Point 1" in rendered
    assert "- [p_alice] Ship feature" in rendered
    assert "- [high] High latency" in rendered
    assert "- [12s] Opening discussion" in rendered
    assert "- [45s] [FFT Analysis] The speaker discussed FFT performance." in rendered
    assert "- [resolved] What about memory? → 2GB" in rendered
    assert "[0s] Hello world" in rendered

def test_build_live_windows_groups_by_meeting_time():
    segments = [
        {"id": "sg_1", "start_s": 0.0, "text": "a"},
        {"id": "sg_2", "start_s": 10.0, "text": "b"},
        {"id": "sg_3", "start_s": 130.0, "text": "c"},
        {"id": "sg_4", "start_s": 250.0, "text": "d"},
    ]
    windows = build_live_windows(segments, 120.0)
    assert [[seg["id"] for seg in w] for w in windows] == [
        ["sg_1", "sg_2"], ["sg_3"], ["sg_4"],
    ]
    assert build_live_windows(segments, 0) == [list(segments)]

def test_host_loads_initial_live_state():
    host = ProductEvalHost(
        "m1",
        [{"id": "sg_1", "start_s": 0.0, "end_s": 1.0, "text": "hi"}],
    )
    base = host.store.snapshot()
    host.store.apply("agent", "agent", [{
        "op": "add_item", "card": "live_notes",
        "text": "The kickoff covered the roadmap.",
        "data": {"heading": "Kickoff", "start_s": 0.0},
        "evidence": ["sg_1"],
    }])
    live_state = host.store.snapshot()

    resumed = ProductEvalHost(
        "m1",
        [{"id": "sg_1", "start_s": 0.0, "end_s": 1.0, "text": "hi"}],
        initial_state=live_state,
    )
    snapshot = resumed.store.snapshot()
    notes = snapshot["cards"]["live_notes"]
    assert len(notes) == 1
    assert notes[0]["text"] == "The kickoff covered the roadmap."
    assert snapshot["seq"] == base["seq"] + 1

def test_strip_proposed_after_redecode_keeps_evidenced_notes_and_touched_items():
    host = ProductEvalHost(
        "m1",
        [
            {"id": "sg_1", "start_s": 0.0, "end_s": 10.0, "text": "hi"},
            {"id": "sg_2", "start_s": 100.0, "end_s": 110.0, "text": "later"},
        ],
    )
    host.allow_agent_writes()
    host.store.apply("agent", "agent", [
        {"op": "add_item", "card": "key_points", "text": "Point",
         "evidence": ["sg_1"]},
        {"op": "add_item", "card": "live_notes", "text": "Note",
         "data": {"heading": "H", "start_s": 0.0}, "evidence": ["sg_2"]},
    ])
    items = host.store.snapshot()["cards"]
    key_point = items["key_points"][0]
    host.store.apply("user", "u1", [{
        "op": "update_item", "id": key_point["id"],
        "base_revision": key_point["revision"], "set": {"text": "Edited"},
    }])
    host.store.apply("agent", "agent", [
        {"op": "add_item", "card": "decisions", "text": "Decide",
         "evidence": ["sg_2"]},
    ])

    # Re-decode covers 0-10s with a brand-new id; nothing covers 100-110s.
    redecoded = ProductEvalHost(
        "m1",
        [{"id": "sg_new", "start_s": 0.0, "end_s": 10.0, "text": "hi"}],
        initial_state=host.store.snapshot(),
    )
    removed = strip_proposed_after_redecode(
        redecoded,
        old_segments=[
            {"id": "sg_1", "start_s": 0.0, "end_s": 10.0},
            {"id": "sg_2", "start_s": 100.0, "end_s": 110.0},
        ],
        new_segments=[{"id": "sg_new", "start_s": 0.0, "end_s": 10.0}],
    )
    cards = redecoded.store.snapshot()["cards"]
    assert removed == 1  # the decision whose only anchor died
    assert cards["decisions"][0]["status"] == "removed"
    # The key point's anchor was remapped onto the new transcript, so it
    # survives for consolidation to reconcile.
    assert cards["key_points"][0]["status"] != "removed"
    assert cards["key_points"][0]["text"] == "Edited"
    assert cards["key_points"][0]["evidence"] == ["sg_new"]
    # live_notes are never stripped, even with dead anchors.
    assert len(cards["live_notes"]) == 1
    assert cards["live_notes"][0]["status"] != "removed"

