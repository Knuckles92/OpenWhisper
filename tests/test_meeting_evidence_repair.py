"""Unit tests for tolerant evidence-id repair (meeting.agent.evidence)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.agent.evidence import repair_evidence_ids


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
