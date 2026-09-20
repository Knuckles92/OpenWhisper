"""Export compatibility for features added after the original export dialogs."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from meeting.export.bulk import collect_meeting_export, render_export_document
from meeting.export.json_export import export_json
from meeting.export.markdown import export_markdown
from meeting.export.transcript_txt import export_transcript_txt, format_meeting_date, resolve_title
from tests.test_meeting_export import _fixture
from tests.test_meeting_repository import make_meeting, make_segment
from tests.test_meeting_web_auth import client  # noqa: F401 -- shared endpoint fixture


def _recent_features():
    meeting, state, segments = _fixture()
    action = state["cards"]["action_items"][0]
    action.update(revision=2, review={"state": "human"},
                  citation_check={"status": "contradicted", "revision": 2})
    action["data"]["deadline"] = "Friday"
    state["insight_review"] = {"questions": [
        {"correction": "The RFC is due Friday.", "status": "answered"},
        {"correction": "OUTDATED CLARIFICATION", "superseded": True},
    ]}
    state["live_highlights"] = [
        {"id": "late", "kind": "number", "start_s": 90, "text": "Budget is 5000",
         "segment_id": "sg_2", "probability": 0.9,
         "assessment": {"threshold": 0.8, "scores": {"number": 0.9}}},
        {"id": "lesson", "kind": "takeaway", "start_s": 45,
         "text": "Early customer feedback prevents rework", "segment_id": "sg_1", "probability": 0.93},
        {"id": "early", "kind": "decision", "start_s": 5, "text": "Choose June",
         "segment_id": "sg_1", "probability": 0.95},
    ]
    state["cards"]["live_notes"] = [
        {"id": "late-note", "card": "live_notes", "status": "proposed",
         "author_type": "system", "author_id": "voice_command",
         "text": "Keep the budget", "data": {"heading": "Budget", "start_s": 90}},
        {"id": "early-note", "card": "live_notes", "status": "proposed",
         "text": "Opening note", "data": {"start_s": 5}},
        {"id": "removed-note", "status": "removed", "text": "REMOVED NOTE"},
    ]
    return meeting, state, segments


def test_markdown_keeps_recent_features_without_mutating_snapshot():
    meeting, state, segments = _recent_features()
    original = copy.deepcopy(state)
    md = export_markdown(meeting, state, segments)
    assert "## Meeting Pulses" in md
    assert "[00:05] **Decision:** Choose June" in md
    assert "[01:30] **Number:** Budget is 5000" in md
    assert "[00:45] **Takeaways:** Early customer feedback prevents rework" in md
    assert md.index("Choose June") < md.index("Early customer feedback") < md.index("Budget is 5000")
    assert "Spoken note: Keep the budget" in md
    assert md.index("Opening note") < md.index("Keep the budget")
    assert "User clarified: Draft RFC" in md
    assert "Due: Friday" in md
    assert "Advisory: Citation conflicts with claim" in md
    assert "The RFC is due Friday." in md
    assert "OUTDATED CLARIFICATION" not in md
    assert "REMOVED NOTE" not in md
    assert state == original


@pytest.mark.parametrize("status,label", [
    ("supported", "Citation supports claim"),
    ("contradicted", "Citation conflicts with claim"),
    ("unsupported", "Check citation"),
    ("missing", "Missing citation"),
    ("uncertain", "Citation uncertain"),
    ("unavailable", "Citation check unavailable"),
    ("stale", "Citation needs recheck"),
])
def test_markdown_citation_badges_only_describe_current_revision(status, label):
    meeting, state, segments = _fixture()
    item = state["cards"]["key_points"][0]
    item.update(revision=3, citation_check={"status": status, "revision": 3})
    assert f"Advisory: {label}" in export_markdown(meeting, state, segments)
    item["revision"] = 4
    assert f"Advisory: {label}" not in export_markdown(meeting, state, segments)


def test_intelligence_toggle_omits_new_features_and_legacy_due_date_is_retained():
    meeting, state, segments = _recent_features()
    data = state["cards"]["action_items"][0]["data"]
    data["due_date"] = data.pop("deadline")
    assert "Due: Friday" in export_markdown(meeting, state, segments)
    md = export_markdown(meeting, state, segments, include_intelligence=False)
    for text in ("Meeting Pulses", "Budget is 5000", "Early customer feedback prevents rework", "Spoken note", "User Clarifications", "Due: Friday", "Advisory:"):
        assert text not in md
    assert "Welcome everyone" in md


def test_json_preserves_all_new_state_and_raw_transcript():
    meeting, state, segments = _recent_features()
    segments[0]["original_text"] = "Original recognizer text"
    payload = json.loads(export_json(meeting, state, segments))
    assert payload["state"] == state
    assert payload["segments"] == segments
    assert "host_token" not in payload["meeting"]
    assert "guest_token" not in payload["meeting"]


@pytest.mark.parametrize("author", ["user", "voice_command"])
def test_bulk_exports_corrected_speech_and_json_preserves_raw_text(repo, author):
    meeting_id = make_meeting(repo)
    repo.add_segments([make_segment(meeting_id, text="Myra will send it")])
    note = {
        "id": "fix", "card": "user_notes", "text": "Correct the name",
        "status": "edited", "author_type": "user" if author == "user" else "system",
        "author_id": None if author == "user" else "voice_command",
        "data": {"kind": "term_correction", "selected_text": "Myra", "replacement": "Maya",
                 "source": "voice_command", "command": "fix_transcript"},
    }
    state = {"seq": 1, "cards": {"user_notes": [note]}}
    repo.persist_state(meeting_id, state)
    entry = collect_meeting_export(repo, meeting_id)
    for fmt in ("markdown", "txt"):
        assert "Maya will send it" in render_export_document([entry], fmt)
    payload = json.loads(render_export_document([entry], "json"))["meetings"][0]
    assert payload["segments"][0]["text"] == "Maya will send it"
    assert payload["segments"][0]["original_text"] == "Myra will send it"
    assert "host-token" not in json.dumps(payload)
    note["status"] = "removed"
    state["seq"] = 2
    repo.persist_state(meeting_id, state)
    restored = collect_meeting_export(repo, meeting_id)
    assert "Myra will send it" in render_export_document([restored], "txt")


def test_utc_headers_use_local_display_and_legacy_dates_still_work():
    started = datetime(2026, 9, 20, 1, 30, tzinfo=timezone.utc)
    meeting = {"started_at": started.isoformat()}
    expected = started.astimezone()
    assert format_meeting_date(meeting) == expected.strftime("%Y-%m-%d %H:%M")
    assert resolve_title(meeting, {}) == expected.strftime("Meeting %Y-%m-%d")
    assert format_meeting_date({"started_at": "2026-03-15T14:30:00"}) == "2026-03-15 14:30"
    assert expected.strftime("%Y-%m-%d %H:%M") in export_transcript_txt(meeting, {}, [])


def test_meeting_spanning_legacy_and_utc_timestamps_exports_without_crashing():
    start = datetime(2026, 9, 19, 9, 0)
    end = (start.astimezone(timezone.utc) + timedelta(minutes=30)).isoformat()
    md = export_markdown({"started_at": start.isoformat(), "ended_at": end}, {}, [])
    assert "Duration 00:30:00" in md

def test_history_export_preserves_every_persisted_field_and_upload_source():
    from services.history_export import render_entry_document, serialize_history_entry
    from services.models import TranscriptionHistory

    entry = TranscriptionHistory.create(
        text="Cleaned transcript", raw_text="Raw transcript", model="parakeet",
        cleanup_provider="ollama", cleanup_model="qwen", source_name="Parts 1–3.wav",
        audio_file="recording.wav", transcription_time=1.5, audio_duration=90, file_size=1024,
    )
    serialized = serialize_history_entry(entry)
    payload = json.loads(render_entry_document(serialized, "json"))
    assert set(payload) == set(TranscriptionHistory.__table__.columns.keys())
    for key, value in payload.items():
        assert value == getattr(entry, key)
    for fmt in ("markdown", "txt"):
        text = render_entry_document(serialized, fmt)
        assert "Parts 1–3.wav" in text
        assert "Cleaned transcript" in text and "Raw transcript" in text
        assert "ollama" in text and "qwen" in text

@pytest.mark.parametrize("fmt", ["md", "json", "txt"])
def test_web_downloads_preserve_current_features_and_match_bulk_content(client, fmt):  # noqa: F811 -- pytest injects the imported endpoint fixture
    from meeting.export.bulk import render_meeting_document
    from tests.test_meeting_web_auth import HOST_TOKEN

    tc, engine, repo = client
    _, state, segments = _recent_features()
    state["meeting_id"] = "m_test"
    engine.store._state.update(state)
    repo._meeting.update(
        state_json=json.dumps(state), state_seq=9,
        agent_provider="ollama", agent_model="qwen",
        agent_endpoint_json='{"base_url":"http://localhost:11434/v1"}',
    )
    repo._segments = [{**segment, "meeting_id": "m_test"} for segment in segments]
    response = tc.get(f"/api/export/{fmt}", params={"token": HOST_TOKEN})
    assert response.status_code == 200
    assert "attachment;" in response.headers["content-disposition"]
    entry = collect_meeting_export(repo, "m_test")
    if fmt == "json":
        payload = response.json()
        assert payload["state"] == engine.store.snapshot()
        for key in ("agent_provider", "agent_model", "agent_endpoint_json", "state_seq"):
            assert payload["meeting"][key] == entry["meeting"][key]
        assert "host_token" not in payload["meeting"]
        assert "guest_token" not in payload["meeting"]
    else:
        assert response.text == render_meeting_document(entry, "markdown" if fmt == "md" else fmt)
