"""Tests for the developer-mode canned meeting fixture."""

import pytest


from meeting.dev_fixture import DEMO_TITLE, seed_demo_meeting
from meeting.state.schema import MeetingState, new_id
from meeting.state.store import MeetingStateStore


def test_seed_demo_meeting_writes_transcript_and_cards(repo):
    meeting_id = new_id("m")
    repo.create_meeting(
        id=meeting_id,
        title="",
        status="active",
        started_at="2026-08-13T00:00:00",
        ended_at=None,
        paused_total_s=0.0,
        host_token="host",
        guest_token="guest",
        cloud_enabled=True,
        asr_model="base",
        agent_provider="openrouter",
        agent_model="test",
        spool_dir="",
        state_json=None,
        state_seq=0,
        app_pid=1,
        app_heartbeat_at="2026-08-13T00:00:00",
    )
    state = MeetingState(meeting_id=meeting_id, cloud_enabled=True, title="")
    store = MeetingStateStore(
        state,
        repository=repo,
        segment_exists=lambda sg_id: repo.segment_exists(meeting_id, sg_id),
    )
    me = store.apply("system", None, [{
        "op": "upsert_participant", "display_name": "Me",
        "kind": "me", "is_provisional": False,
    }])[0]
    assert me.ok

    result = seed_demo_meeting(
        meeting_id=meeting_id,
        me_participant_id=me.target_id,
        store=store,
        repository=repo,
    )

    assert result["segment_count"] >= 10
    assert result["last_end_s"] > 60
    segments = repo.get_segments(meeting_id)
    assert len(segments) == result["segment_count"]
    snapshot = store.snapshot()
    assert snapshot["title"] == DEMO_TITLE
    assert snapshot["topic"]["current"]
    assert snapshot["rolling_summary"]
    assert snapshot["cards"]["key_points"]
    decisions = [
        item for item in snapshot["cards"]["decisions"]
        if item.get("status") != "removed"
    ]
    assert decisions
    assert decisions[0]["pinned"] is True
    assert decisions[0]["status"] == "confirmed"
    assert snapshot["questions"]
