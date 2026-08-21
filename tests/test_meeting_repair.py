"""Tests for deterministic meeting-state repairs and evidence-id repair."""

from meeting.agent.evidence import repair_evidence_ids
from meeting.state.repair import (
    build_summary_backfill_ops,
    build_timeline_backfill_ops,
    build_topic_backfill_ops,
    repair_meeting_state,
)
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore


def test_build_ops_noop_when_timeline_populated():
    state = {
        "cards": {
            "key_points": [],
            "timeline": [
                {"id": "it_1", "text": "Already there", "status": "proposed",
                 "data": {"start_s": 0.0}, "evidence": ["sg_1"]},
            ],
        }
    }
    assert build_timeline_backfill_ops(state, []) == []


def test_build_ops_promotes_key_points_with_evidence():
    state = {
        "cards": {
            "key_points": [
                {"id": "it_a", "text": "Apple example", "status": "proposed",
                 "evidence": ["sg_late", "sg_early"]},
                {"id": "it_b", "text": "No anchors", "status": "proposed",
                 "evidence": []},
            ],
            "timeline": [],
        }
    }
    segments = [
        {"id": "sg_early", "start_s": 5.0, "text": "early"},
        {"id": "sg_late", "start_s": 40.0, "text": "late"},
    ]
    ops = build_timeline_backfill_ops(state, segments)
    assert len(ops) == 1
    assert ops[0]["card"] == "timeline"
    assert ops[0]["text"] == "Apple example"
    assert ops[0]["data"]["start_s"] == 5.0


def test_build_ops_prepends_opening_when_key_points_start_late():
    state = {
        "cards": {
            "key_points": [
                {"id": "it_a", "text": "Apple example", "status": "proposed",
                 "evidence": ["sg_apple"]},
            ],
            "timeline": [],
        }
    }
    segments = [
        {"id": "sg_open", "start_s": 0.0, "text": "How do you explain assumptions?"},
        {"id": "sg_apple", "start_s": 27.7, "text": "Why is Apple innovative?"},
    ]
    ops = build_timeline_backfill_ops(state, segments)
    assert [op["data"]["start_s"] for op in ops] == [0.0, 27.7]
    assert "assumptions" in ops[0]["text"].lower()


def test_build_ops_falls_back_to_segment_windows():
    state = {"cards": {"key_points": [], "timeline": []}}
    segments = [
        {"id": "sg_0", "start_s": 0.0, "text": "Opening question"},
        {"id": "sg_1", "start_s": 8.0, "text": "Still opening"},
        {"id": "sg_2", "start_s": 25.0, "text": "Middle example"},
        {"id": "sg_3", "start_s": 50.0, "text": "Closing discovery"},
    ]
    ops = build_timeline_backfill_ops(state, segments)
    assert [op["data"]["start_s"] for op in ops] == [0.0, 25.0, 50.0]
    assert ops[0]["evidence"] == ["sg_0"]


def test_build_summary_ops_from_key_points_when_empty():
    state = {
        "rolling_summary": "",
        "cards": {
            "key_points": [
                {"text": "Apple innovates", "status": "proposed",
                 "evidence": ["sg_1"]},
                {"text": "Wright brothers flew", "status": "proposed",
                 "evidence": ["sg_2"]},
            ],
        },
    }
    ops = build_summary_backfill_ops(state, [])
    assert len(ops) == 1
    assert ops[0]["op"] == "set_rolling_summary"
    assert "Apple innovates" in ops[0]["text"]
    assert "Wright brothers flew" in ops[0]["text"]


def test_build_summary_ops_noop_when_present():
    state = {"rolling_summary": "Already written.", "cards": {"key_points": []}}
    assert build_summary_backfill_ops(state, []) == []


def test_build_topic_ops_from_first_key_point():
    state = {
        "topic": {"current": ""},
        "cards": {
            "key_points": [
                {"text": "Why do some defy assumptions?", "status": "proposed",
                 "evidence": ["sg_1"]},
            ],
        },
    }
    ops = build_topic_backfill_ops(state, [])
    assert len(ops) == 1
    assert ops[0]["op"] == "set_topic"
    assert "assumptions" in ops[0]["text"].lower()


def test_repair_meeting_state_applies_through_store():
    state = MeetingState(meeting_id="m_repair")
    store = MeetingStateStore(state)
    store.apply("agent", "agent", [
        {"op": "add_item", "card": "key_points", "text": "Ship Friday",
         "evidence": ["sg_1"]},
    ])
    # Bypass store evidence checks by injecting a segment map via apply that
    # does not validate existence when segment_exists is None.
    applied = repair_meeting_state(
        store,
        [{"id": "sg_1", "start_s": 3.0, "text": "Ship Friday please"}],
    )
    # Timeline beat + summary + topic composed from the key point.
    assert applied == 3
    snap = store.snapshot()
    timeline = [
        item for item in snap["cards"]["timeline"]
        if item["status"] != "removed"
    ]
    assert len(timeline) == 1
    assert timeline[0]["data"]["start_s"] == 3.0
    assert timeline[0]["author_type"] == "system"
    assert "Ship Friday" in snap["rolling_summary"]


def test_build_keypoint_coverage_promotes_uncovered_timeline_beats():
    from meeting.state.repair import build_keypoint_coverage_ops

    state = {
        "cards": {
            "key_points": [
                {"text": "Apple innovates despite same resources",
                 "status": "proposed", "evidence": ["sg_1"]},
            ],
            "timeline": [
                {"text": "Apple innovates despite same resources",
                 "status": "proposed", "evidence": ["sg_1"]},
                {"text": "Martin Luther King led the Civil Rights Movement",
                 "status": "proposed", "evidence": ["sg_2"]},
            ],
        }
    }
    ops = build_keypoint_coverage_ops(state)
    assert len(ops) == 1
    assert ops[0]["card"] == "key_points"
    assert "martin luther king" in ops[0]["text"].lower()


def test_timeline_coverage_adds_missing_key_point_beats():
    from meeting.state.repair import build_timeline_coverage_from_segments

    state = {
        "cards": {
            "key_points": [
                {"text": "Opening puzzle about assumptions",
                 "status": "proposed", "evidence": ["sg_0"]},
                {"text": "For example, why is Apple so innovative?",
                 "status": "proposed", "evidence": ["sg_a"]},
            ],
            "timeline": [
                {"text": "Opening puzzle about assumptions",
                 "status": "proposed", "evidence": ["sg_0"],
                 "data": {"start_s": 0.0}},
            ],
        }
    }
    segments = [
        {"id": "sg_0", "start_s": 0.0, "text": "Opening"},
        {"id": "sg_a", "start_s": 27.0, "text": "Apple"},
    ]
    ops = build_timeline_coverage_from_segments(state, segments)
    assert len(ops) == 1
    assert ops[0]["data"]["start_s"] == 27.0
    assert "apple" in ops[0]["text"].lower()


def test_build_keypoint_coverage_lifts_named_examples_from_segments():
    from meeting.state.repair import build_keypoint_coverage_ops

    state = {
        "rolling_summary": (
            "Examples include Apple's innovation and Martin Luther King."
        ),
        "topic": {"current": "Why some succeed"},
        "cards": {
            "key_points": [
                {"text": "Opening: why do some defy assumptions?",
                 "status": "proposed", "evidence": ["sg_0"]},
            ],
            "timeline": [],
        },
    }
    segments = [
        {"id": "sg_0", "start_s": 0.0,
         "text": "How do you explain when things don't go as we assume?"},
        {"id": "sg_a", "start_s": 27.0,
         "text": "For example, why is Apple so innovative?"},
        {"id": "sg_m", "start_s": 50.0,
         "text": "Why is it that Martin Luther King led the Civil Rights Movement?"},
    ]
    ops = build_keypoint_coverage_ops(state, segments)
    texts = " ".join(op["text"].lower() for op in ops)
    assert "apple" in texts
    assert "martin luther king" in texts


def test_repair_meeting_state_fills_summary_only():
    state = MeetingState(meeting_id="m_repair")
    store = MeetingStateStore(state)
    store.apply("system", "seed", [
        {"op": "add_item", "card": "timeline", "text": "Opening",
         "data": {"start_s": 0.0}, "evidence": ["sg_1"]},
        {"op": "add_item", "card": "key_points", "text": "A finding",
         "evidence": ["sg_1"]},
    ])
    applied = repair_meeting_state(
        store, [{"id": "sg_1", "start_s": 0.0, "text": "hello"}],
    )
    # Summary + topic (timeline already populated).
    assert applied == 2
    snap = store.snapshot()
    assert "A finding" in snap["rolling_summary"]
    assert snap["topic"]["current"]


def test_repair_skips_timeline_when_ribbon_disabled():
    state = MeetingState(
        meeting_id="m_repair_noribbon",
        report_views=["brief"],
    )
    store = MeetingStateStore(state)
    segments = [
        {"id": "sg_1", "start_s": 0.0, "text": "Let's start with the migration."},
        {"id": "sg_2", "start_s": 25.0, "text": "The benchmarks held under peak load."},
    ]
    applied = repair_meeting_state(store, segments)
    snap = store.snapshot()
    live_timeline = [
        item for item in snap["cards"]["timeline"]
        if item.get("status") != "removed"
    ]
    assert live_timeline == []
    assert applied >= 1
    assert snap["rolling_summary"] or snap["topic"]["current"]


def test_build_summary_ops_from_live_notes_when_key_points_empty():
    state = {
        "rolling_summary": "",
        "cards": {
            "key_points": [],
            "live_notes": [
                {
                    "text": "Team reviewed Q3 metrics and conversion rates.",
                    "status": "proposed",
                    "data": {"heading": "Funnel metrics", "start_s": 12.0},
                    "evidence": ["sg_1"],
                },
                {
                    "text": "Agreed to migrate OAuth provider by next sprint.",
                    "status": "proposed",
                    "data": {"heading": "OAuth Migration", "start_s": 45.0},
                    "evidence": ["sg_2"],
                },
            ],
        },
    }
    ops = build_summary_backfill_ops(state, [])
    assert len(ops) == 1
    assert ops[0]["op"] == "set_rolling_summary"
    assert "Q3 metrics" in ops[0]["text"]
    assert "OAuth provider" in ops[0]["text"]


def test_build_topic_ops_from_live_notes_when_key_points_empty():
    state = {
        "topic": {"current": ""},
        "cards": {
            "key_points": [],
            "live_notes": [
                {
                    "text": "Maria walked through the quarterly revenue numbers.",
                    "status": "proposed",
                    "data": {"heading": "Quarterly Revenue Review", "start_s": 5.0},
                    "evidence": ["sg_1"],
                },
            ],
        },
    }
    ops = build_topic_backfill_ops(state, [])
    assert len(ops) == 1
    assert ops[0]["op"] == "set_topic"
    assert ops[0]["text"] == "Quarterly Revenue Review"


def test_build_timeline_ops_from_live_notes_when_key_points_empty():
    state = {
        "cards": {
            "key_points": [],
            "timeline": [],
            "live_notes": [
                {
                    "text": "Introduced new deployment strategy.",
                    "status": "proposed",
                    "data": {"heading": "Deployment overview", "start_s": 10.0},
                    "evidence": ["sg_1"],
                },
                {
                    "text": "Discussed rollbacks and canary releases.",
                    "status": "proposed",
                    "data": {"heading": "Canary & Rollback", "start_s": 60.0},
                    "evidence": ["sg_2"],
                },
            ],
        },
    }
    segments = [
        {"id": "sg_1", "start_s": 10.0, "text": "Deployment strategy"},
        {"id": "sg_2", "start_s": 60.0, "text": "Canary testing"},
    ]
    ops = build_timeline_backfill_ops(state, segments)
    assert len(ops) >= 2
    assert [op["data"]["start_s"] for op in ops if op["data"]["start_s"] >= 10.0] == [10.0, 60.0]
    texts = " ".join(op["text"] for op in ops)
    assert "Deployment" in texts
    assert "Canary" in texts


def test_build_keypoint_coverage_from_live_notes():
    from meeting.state.repair import build_keypoint_coverage_ops

    state = {
        "rolling_summary": "Discussed infrastructure upgrades.",
        "topic": {"current": "Infrastructure"},
        "cards": {
            "key_points": [],
            "timeline": [],
            "live_notes": [
                {
                    "text": "Database read replicas reduced latency by forty percent.",
                    "status": "proposed",
                    "data": {"heading": "Database Latency", "start_s": 30.0},
                    "evidence": ["sg_3"],
                },
            ],
        },
    }
    ops = build_keypoint_coverage_ops(state, [])
    assert len(ops) >= 1
    assert any("Database Latency" in op["text"] or "latency" in op["text"] for op in ops)



# --------------------------------------------------------------------------
# Tolerant evidence-id repair (meeting.agent.evidence).
# --------------------------------------------------------------------------

KNOWN = [
    "sg_5f2c721de88ac0aa2321",
    "sg_e991e94d6decd27d036a",
    "sg_025f2c0b2c30e3e8dcb3",
    "sg_5f2c721de88ac0aa2329",
]


def _exists(sid: str) -> bool:
    return sid in KNOWN


def test_truncated_id_repaired_to_unique_prefix():
    ops, count = repair_evidence_ids(
        [{"op": "add_item", "evidence": ["sg_e991e94d6decd27d03"]}],
        KNOWN, _exists,
    )
    assert count == 1
    assert ops[0]["evidence"] == ["sg_e991e94d6decd27d036a"]


def test_one_char_typo_repaired_by_fuzzy_match():
    ops, count = repair_evidence_ids(
        [{"op": "set_topic", "evidence": ["sg_025f2c0b2c30e3e8dcb4"]}],
        KNOWN, _exists,
    )
    assert count == 1
    assert ops[0]["evidence"] == ["sg_025f2c0b2c30e3e8dcb3"]


def test_reconstructed_id_with_several_wrong_chars_repaired():
    # Observed failure mode (IN1009 consolidation): the model rebuilds an id
    # from memory with 4-6 wrong characters mid-string — similarity ~0.74
    # against the intended id while every other id sits below 0.4, so the
    # margin guard still identifies it unambiguously.
    ops, count = repair_evidence_ids(
        [{"op": "set_topic", "evidence": ["sg_97bd44598ee4f6c7091f"]}],
        KNOWN + ["sg_97bd44598ee4da9dc5f9"], _exists,
    )
    assert count == 1
    assert ops[0]["evidence"] == ["sg_97bd44598ee4da9dc5f9"]


def test_ambiguous_mid_string_corruption_left_alone():
    # When two citable ids differ only in their tail, a corrupted id that
    # lands between them must not be guessed onto either.
    ops, count = repair_evidence_ids(
        [{"op": "set_topic", "evidence": ["sg_5f2c921de38ac0af2321"]}],
        KNOWN, _exists,
    )
    assert count == 0
    assert ops[0]["evidence"] == ["sg_5f2c921de38ac0af2321"]


def test_ambiguous_prefix_left_alone():
    # Both sg_5f2c... ids share the truncated prefix — must not guess.
    ops, count = repair_evidence_ids(
        [{"op": "add_item", "evidence": ["sg_5f2c721de88ac0aa23"]}],
        KNOWN, _exists,
    )
    assert count == 0
    assert ops[0]["evidence"] == ["sg_5f2c721de88ac0aa23"]


def test_invented_id_left_for_honest_rejection():
    ops, count = repair_evidence_ids(
        [{"op": "add_item", "evidence": ["sg_ffffffffffffffffffff"]}],
        KNOWN, _exists,
    )
    assert count == 0
    assert ops[0]["evidence"] == ["sg_ffffffffffffffffffff"]


def test_existing_and_non_sg_ids_untouched():
    # Known ids and unknown non-sg string ids are preserved; non-strings dropped.
    ops, count = repair_evidence_ids(
        [{"op": "add_item", "evidence": [KNOWN[0], "q_123", 7]}],
        KNOWN, _exists,
    )
    assert count == 0
    assert ops[0]["evidence"] == [KNOWN[0], "q_123"]


def test_repair_counts_each_id_once_and_caches():
    ops, count = repair_evidence_ids(
        [
            {"op": "add_item", "evidence": ["sg_025f2c0b2c30e3e8dcb4"]},
            {"op": "add_item", "evidence": ["sg_025f2c0b2c30e3e8dcb4"]},
        ],
        KNOWN, _exists,
    )
    assert count == 2
    assert all(
        op["evidence"] == ["sg_025f2c0b2c30e3e8dcb3"] for op in ops
    )


def test_direct_core_repairs_before_dispatch():
    from meeting.agent.openrouter_direct import DirectOpenRouterAgent

    class FakeHost:
        def segment_exists(self, sid: str) -> bool:
            return sid in KNOWN

    agent = DirectOpenRouterAgent()
    agent._tools = FakeHost()  # type: ignore[assignment]
    agent._citable_ids = list(KNOWN)
    repaired = agent._repair_ops([
        {"op": "add_item", "card": "decisions", "text": "x",
         "evidence": ["sg_e991e94d6decd27d03"]},
    ])
    assert repaired[0]["evidence"] == ["sg_e991e94d6decd27d036a"]

    # Without a citable universe (no payload) nothing is touched.
    agent._citable_ids = []
    raw = [{"op": "add_item", "evidence": ["sg_e991e94d6decd27d03"]}]
    assert agent._repair_ops(raw) is raw
