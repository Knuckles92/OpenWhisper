"""
Tests for meeting exporters: markdown/json/txt shape and token stripping.
"""
import json

import pytest

from meeting.export.json_export import FORMAT_VERSION, export_json
from meeting.export.markdown import export_markdown
from meeting.export.transcript_txt import export_transcript_txt

def _fixture():
    meeting = {
        "id": "m_export",
        "title": "Planning Sync",
        "status": "ended",
        "started_at": "2026-03-15T14:30:00",
        "ended_at": "2026-03-15T15:00:00",
        "paused_total_s": 0.0,
        "cloud_enabled": False,
        "asr_model": "base",
        "host_token": "SECRET_HOST_TOKEN_XXXX",
        "guest_token": "SECRET_GUEST_TOKEN_YYYY",
        "app_pid": 12345,
        "app_heartbeat_at": "2026-03-15T15:00:00",
        "state_json": "{\"seq\": 9}",
        "spool_dir": "/tmp/spool",
    }
    state = {
        "meeting_id": "m_export",
        "seq": 9,
        "title": "Planning Sync",
        "status": "ended",
        "topic": {
            "current": "Q3 roadmap",
            "history": [
                {"text": "Kickoff", "ts": "2026-03-15T14:30:00"},
                {"text": "Q3 roadmap", "ts": "2026-03-15T14:45:00"},
            ],
        },
        "rolling_summary": "Team aligned on milestones.",
        "cards": {
            "key_points": [
                {"id": "it_1", "card": "key_points", "text": "Ship beta in June",
                 "status": "proposed", "data": {}, "pinned": False,
                 "evidence": ["sg_1"]},
            ],
            "decisions": [
                {"id": "it_2", "card": "decisions", "text": "Use FastAPI",
                 "status": "confirmed", "data": {}, "pinned": True,
                 "evidence": []},
            ],
            "action_items": [
                {"id": "it_3", "card": "action_items", "text": "Draft RFC",
                 "status": "proposed",
                 "data": {"owner_participant_id": "p_me"}, "pinned": False,
                 "evidence": []},
            ],
            "risks": [
                {"id": "it_4", "card": "risks", "text": "Vendor delay",
                 "status": "proposed", "data": {"severity": "high"},
                 "pinned": False, "evidence": []},
            ],
            "timeline": [
                {"id": "it_5", "card": "timeline", "text": "Kickoff done",
                 "status": "proposed", "data": {"start_s": 90},
                 "pinned": False, "evidence": []},
            ],
            "user_notes": [
                {"id": "it_gone", "card": "user_notes", "text": "scratch",
                 "status": "removed", "data": {}, "pinned": False,
                 "evidence": []},
            ],
        },
        "participants": {
            "p_me": {"id": "p_me", "display_name": "Alex", "kind": "me"},
            "p_2": {"id": "p_2", "display_name": "Sam", "kind": "guest"},
        },
        "questions": [
            {"id": "q_1", "text": "Deadline firm?", "status": "open",
             "suggested_answer": "Yes, June 30", "suggested_confidence": 0.6},
            {"id": "q_2", "text": "Who owns RFC?", "status": "resolved",
             "answer": "Alex", "answer_source": "audio",
             "resolved_by": "p_me"},
            {"id": "q_3", "text": "Ignored", "status": "dismissed"},
        ],
        "cloud_enabled": False,
        "intelligence_online": True,
    }
    segments = [
        {"id": "sg_1", "start_s": 5.0, "end_s": 8.0, "text": "Welcome everyone",
         "channel": "mic", "speaker_participant_id": "p_me"},
        {"id": "sg_2", "start_s": 10.0, "end_s": 14.0, "text": "Let's plan Q3",
         "channel": "loopback", "speaker_participant_id": "p_2"},
    ]
    return meeting, state, segments

class TestJsonExport:
    def test_strips_tokens_and_volatile_fields(self):
        meeting, state, segments = _fixture()
        raw = export_json(meeting, state, segments)
        data = json.loads(raw)
        assert data["format_version"] == FORMAT_VERSION
        assert data["meeting"]["id"] == "m_export"
        assert data["meeting"]["title"] == "Planning Sync"
        for key in ("host_token", "guest_token", "app_pid",
                    "app_heartbeat_at", "state_json"):
            assert key not in data["meeting"]
        assert "SECRET_HOST_TOKEN_XXXX" not in raw
        assert "SECRET_GUEST_TOKEN_YYYY" not in raw
        assert data["state"]["topic"]["current"] == "Q3 roadmap"
        assert len(data["segments"]) == 2
        assert data["segments"][0]["text"] == "Welcome everyone"

    def test_preserves_evidence_ids(self):
        meeting, state, segments = _fixture()
        data = json.loads(export_json(meeting, state, segments))
        kp = data["state"]["cards"]["key_points"][0]
        assert kp["evidence"] == ["sg_1"]

class TestMarkdownExport:
    def test_golden_structure(self):
        meeting, state, segments = _fixture()
        md = export_markdown(meeting, state, segments)
        assert md.startswith("# Planning Sync\n")
        assert "2026-03-15 14:30" in md
        assert "Participants: Alex, Sam" in md
        assert "## Topic" in md
        assert "Q3 roadmap" in md
        assert "## Summary" in md
        assert "Team aligned on milestones." in md
        assert "## Key Points" in md
        assert "- [00:05] Ship beta in June" in md
        assert "## Decisions" in md
        assert "- Use FastAPI" in md
        assert "## Action Items" in md
        assert "**Mine**" in md
        assert "- Draft RFC" in md
        assert "## Risks" in md
        assert "**[high]**" in md
        assert "## Timeline" in md
        assert "[01:30]" in md
        assert "## Questions" in md
        assert "**Open:** Deadline firm?" in md
        assert "**Resolved:** Who owns RFC?" in md
        assert "answered from audio" in md
        assert "Ignored" not in md  # dismissed omitted
        assert "scratch" not in md  # removed notes omitted
        assert "### Transcript" in md
        assert "[00:00:05] Alex: Welcome everyone" in md
        assert "[00:00:10] Sam: Let's plan Q3" in md
        assert md.endswith("\n")

    def test_can_omit_transcript_and_intelligence(self):
        meeting, state, segments = _fixture()
        md = export_markdown(
            meeting,
            state,
            segments,
            include_transcript=False,
            include_intelligence=False,
        )
        assert md.startswith("# Planning Sync\n")
        assert "## Key Points" not in md
        assert "## Summary" not in md
        assert "### Transcript" not in md
        assert "Welcome everyone" not in md
        assert "Ship beta in June" not in md
        assert "2026-03-15 14:30" in md

class TestTranscriptTxtExport:
    def test_plain_transcript(self):
        meeting, state, segments = _fixture()
        txt = export_transcript_txt(meeting, state, segments)
        assert txt.startswith("Planning Sync\n")
        assert "2026-03-15 14:30" in txt
        assert "Participants: Alex, Sam" in txt
        assert "[00:00:05] Alex: Welcome everyone" in txt
        assert "[00:00:10] Sam: Let's plan Q3" in txt
        assert "SECRET_HOST" not in txt
        assert txt.endswith("\n")

    def test_empty_segments_placeholder(self):
        meeting, state, _ = _fixture()
        txt = export_transcript_txt(meeting, state, [])
        assert "(no transcript)" in txt
