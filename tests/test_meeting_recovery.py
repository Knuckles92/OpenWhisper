"""Focused tests for Meeting Mode crash-recovery finalization patching."""
import json

from meeting.recovery import _mark_ended
from meeting.state.schema import MeetingState


class FakeRepository:
    def __init__(self):
        self.updates = []

    def update_meeting(self, meeting_id, **fields):
        self.updates.append((meeting_id, fields))


def test_mark_ended_normalizes_interrupted_finalization():
    """Headless ASR finalize must leave a durable finalization outcome."""
    state = MeetingState(
        meeting_id="m_crash",
        title="Crash mid-finalize",
        status="active",
        cloud_enabled=True,
    )
    payload = state.to_dict()
    payload["finalization"] = {
        "status": "running",
        "message": "Preparing final cloud insights…",
    }
    meeting = {
        "id": "m_crash",
        "cloud_enabled": True,
        "state_json": json.dumps(payload),
    }
    repo = FakeRepository()

    _mark_ended(repo, meeting)

    assert len(repo.updates) == 1
    meeting_id, fields = repo.updates[0]
    assert meeting_id == "m_crash"
    assert fields["status"] == "ended"
    assert "ended_at" in fields
    patched = json.loads(fields["state_json"])
    assert patched["status"] == "ended"
    assert patched["finalization"]["status"] == "failed"
    assert "interrupted" in patched["finalization"]["message"].lower()


def test_mark_ended_pending_becomes_unavailable():
    state = MeetingState(
        meeting_id="m_pending",
        status="paused",
        cloud_enabled=True,
    )
    payload = state.to_dict()
    payload["finalization"] = {"status": "pending", "message": ""}
    meeting = {
        "id": "m_pending",
        "cloud_enabled": True,
        "state_json": json.dumps(payload),
    }
    repo = FakeRepository()

    _mark_ended(repo, meeting)

    patched = json.loads(repo.updates[0][1]["state_json"])
    assert patched["finalization"]["status"] == "unavailable"


def test_mark_ended_cloud_off_becomes_disabled():
    state = MeetingState(
        meeting_id="m_off",
        status="active",
        cloud_enabled=False,
    )
    payload = state.to_dict()
    payload["finalization"] = {"status": "pending", "message": ""}
    meeting = {
        "id": "m_off",
        "cloud_enabled": False,
        "state_json": json.dumps(payload),
    }
    repo = FakeRepository()

    _mark_ended(repo, meeting)

    patched = json.loads(repo.updates[0][1]["state_json"])
    assert patched["finalization"]["status"] == "disabled"
