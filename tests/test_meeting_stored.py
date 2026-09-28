"""Tests for loading persisted meetings through meeting.stored."""
import json

from meeting.state.schema import MeetingState
from meeting.stored import load_state, open_store, stored_state_dict
from tests.helpers import make_meeting, make_segment


def _row(state, **fields):
    row = {
        "id": "m_row",
        "title": "Row title",
        "status": "ended",
        "cloud_enabled": True,
        "state_seq": 9,
        "state_json": json.dumps(state) if isinstance(state, dict) else state,
    }
    row.update(fields)
    return row


def _interrupted_snapshot():
    state = MeetingState(
        meeting_id="m_row", title="Snapshot title", status="ending",
        cloud_enabled=False, seq=4,
    ).to_dict()
    state["finalization"] = {"status": "running", "message": "Writing insights"}
    state["insight_review"] = {"status": "running", "questions": []}
    state["custom_reports"] = [
        {"id": "rp_running", "request": "Budget recap", "status": "running"},
        {"id": "rp_ready", "request": "Owners", "status": "ready", "body": "Done"},
    ]
    return state


def test_stored_state_dict_tolerates_missing_and_unreadable_snapshots():
    assert stored_state_dict(None) == {}
    assert stored_state_dict({"state_json": None}) == {}
    assert stored_state_dict({"state_json": "{broken"}) == {}
    assert stored_state_dict({"state_json": "[1, 2]"}) == {}
    assert stored_state_dict({"state_json": '{"title": "x"}'}) == {"title": "x"}


def test_historical_load_prefers_the_row_and_stops_interrupted_work():
    state = load_state(_row(_interrupted_snapshot()), historical=True)

    assert (state.meeting_id, state.title, state.status) == (
        "m_row", "Row title", "ended",
    )
    assert state.cloud_enabled is True
    assert state.seq == 4
    assert state.finalization.status == "failed"
    assert state.insight_review["status"] == "unavailable"
    reports = {report.id: report for report in state.custom_reports}
    assert reports["rp_running"].status == "failed"
    assert "interrupted" in reports["rp_running"].message
    assert reports["rp_ready"].status == "ready"


def test_historical_load_of_an_unreadable_snapshot_keeps_row_identity():
    state = load_state(_row("{broken"), historical=True)

    assert (state.meeting_id, state.title, state.status) == (
        "m_row", "Row title", "ended",
    )
    assert state.cloud_enabled is True
    assert state.seq == 9
    assert state.finalization.status == "unavailable"


def test_working_load_keeps_the_snapshot_as_saved():
    state = load_state(_row(_interrupted_snapshot()), "m_row")

    assert (state.title, state.status) == ("Snapshot title", "ending")
    assert state.finalization.status == "running"
    assert state.custom_reports[0].status == "running"


def test_working_load_of_an_unreadable_snapshot_starts_fresh():
    state = load_state(_row("{broken"), "m_row")

    assert state.meeting_id == "m_row"
    assert state.title == "Row title"
    assert state.cloud_enabled is True
    assert state.cards["key_points"] == []


def test_historical_store_can_correct_a_speaker(repo):
    """History dashboards apply transcript speaker fixes like a live meeting."""
    from meeting.web.archive import ArchivedMeetingDashboard

    meeting_id = make_meeting(repo)
    repo.add_segments([make_segment(meeting_id, "sg_1")])
    repo.update_meeting(meeting_id, status="ended", state_json=json.dumps(
        MeetingState(meeting_id=meeting_id, status="ended").to_dict()
    ))
    archive = ArchivedMeetingDashboard(
        repo, repo.get_meeting(meeting_id), spool_root="meetings",
    )
    guest = archive.add_guest("Priya")

    [result] = archive.apply_client_action("host", None, {
        "op": "reassign_segment_speaker",
        "segment_id": "sg_1",
        "participant_id": guest["id"],
    })

    assert result.ok, result.reason
    assert repo.get_segment(meeting_id, "sg_1")["speaker_participant_id"] == guest["id"]
    reopened = open_store(repo, meeting_id, repo.get_meeting(meeting_id), historical=True)
    assert guest["id"] in reopened.snapshot()["participants"]
