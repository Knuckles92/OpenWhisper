"""Tests for revise_segment_text op validation and store application."""
from __future__ import annotations

from datetime import datetime

import pytest

from meeting.interfaces import TranscriptSegment
from meeting.persist.repository import SqlMeetingRepository
from meeting.state.patches import OpContext, apply_ops
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore
from services.database import DatabaseManager

def _repo(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "meet.db"))
    repo = SqlMeetingRepository(db)
    mid = "m_polish"
    repo.create_meeting(
        id=mid,
        title="t",
        status="active",
        started_at=datetime.now().isoformat(),
        host_token="h",
        guest_token="g",
        cloud_enabled=True,
        spool_dir=str(tmp_path / "spool"),
    )
    repo.add_segments([
        TranscriptSegment(
            segment_id="sg_one",
            meeting_id=mid,
            chunk_id=None,
            channel="mic",
            start_s=1.0,
            end_s=2.0,
            text="helo world",
        )
    ])
    return repo, mid

def test_revise_segment_text_rejects_missing_evidence():
    state = MeetingState(meeting_id="m1", title="t")
    ctx = OpContext(
        "agent", "agent",
        segment_exists=lambda sid: sid == "sg_one",
    )
    results = apply_ops(state, [{
        "op": "revise_segment_text",
        "segment_id": "sg_one",
        "text": "hello world",
        "evidence": ["sg_other"],
    }], ctx)
    assert not results[0].ok
    assert results[0].reason in {"missing_evidence", "unknown_evidence"}

def test_revise_segment_text_rejects_untrusted_human_client():
    state = MeetingState(meeting_id="m1", title="t")
    ctx = OpContext(
        "user", "guest",
        segment_exists=lambda sid: sid == "sg_one",
    )
    results = apply_ops(state, [{
        "op": "revise_segment_text",
        "segment_id": "sg_one",
        "text": "forged replacement",
    }], ctx)
    assert not results[0].ok
    assert results[0].reason == "agent_only"

def test_revise_segment_text_applies_via_store(tmp_path):
    repo, mid = _repo(tmp_path)
    state = MeetingState(meeting_id=mid, title="t")
    store = MeetingStateStore(
        state,
        repository=repo,
        segment_exists=lambda sid: repo.segment_exists(mid, sid),
        segment_handler=lambda result: {
            "op": "revise_segment_text",
            "segment_id": "sg_one",
            "text": "helo world",
            "evidence": ["sg_one"],
        },
    )
    # Enrich effect like MeetingEngine does.
    original_handler = store._segment_handler

    def handler(result):
        effect = result.effect or {}
        prior = repo.get_segment(mid, effect["segment_id"])
        updated = dict(prior)
        updated["text"] = effect["text"]
        result.effect = {
            "entity": "segment_text",
            "segment_id": effect["segment_id"],
            "text": effect["text"],
            "segment": updated,
        }
        return original_handler(result)

    store._segment_handler = handler
    results = store.apply("agent", "agent", [{
        "op": "revise_segment_text",
        "segment_id": "sg_one",
        "text": "hello world",
        "evidence": ["sg_one"],
    }])
    assert results[0].ok
    assert results[0].effect["entity"] == "segment_text"
    row = repo.get_segment(mid, "sg_one")
    assert row["text"] == "hello world"
