"""Consent-gated past-meeting recall: caps, exclusion, opaque refs."""
from __future__ import annotations

from unittest.mock import patch

from meeting.recall import (
    MAX_HIT_LIMIT,
    MAX_SNIPPET_CHARS,
    search_past_meetings,
)

class _Repo:
    def __init__(self, rows=None, meetings=None, segments=None):
        self.rows = list(rows or [])
        self.meetings = dict(meetings or {})
        self.segments = dict(segments or {})
        self.search_kwargs = None

    def search_transcripts(self, query, *, exclude_meeting_id=None, limit=200):
        self.search_kwargs = {
            "query": query,
            "exclude_meeting_id": exclude_meeting_id,
            "limit": limit,
        }
        return list(self.rows)

    def get_meeting(self, meeting_id):
        row = self.meetings.get(meeting_id)
        return dict(row) if row else None

    def get_segments(self, meeting_id, after_start_s=-1.0, limit=None):
        rows = list(self.segments.get(meeting_id, []))
        return rows[:limit] if limit else rows

def _hit(meeting_id="m_past", text="we adopted the budget",
         title="Q1 review", started_at="2026-03-01T10:00:00",
         start_s=12.0, segment_id="sg_abcdef123456"):
    return {
        "segment_id": segment_id,
        "meeting_id": meeting_id,
        "text": text,
        "snippet": text,
        "title": title,
        "started_at": started_at,
        "start_s": start_s,
    }

class TestRecallConsent:
    def test_disabled_when_setting_is_off(self):
        repo = _Repo(rows=[_hit()])
        with patch("services.settings.resolve_meeting_past_recall_enabled",
                   return_value=False):
            result = search_past_meetings(
                repo, query="budget", current_meeting_id="m_live",
            )
        assert result["disabled"] is True
        assert result["ok"] is False
        assert result["hits"] == []
        assert "disabled" in result["text"].lower()
        assert repo.search_kwargs is None

    def test_missing_repository_is_disabled(self):
        with patch("services.settings.resolve_meeting_past_recall_enabled",
                   return_value=True):
            result = search_past_meetings(
                None, query="budget", current_meeting_id="m_live",
            )
        assert result["disabled"] is True

class TestRecallSearch:
    def test_excludes_current_meeting_and_omits_sg_ids(self):
        repo = _Repo(rows=[
            _hit("m_live", "live budget talk"),
            _hit("m_past", "we adopted the budget",
                 segment_id="sg_deadbeef9999"),
        ])
        with patch("services.settings.resolve_meeting_past_recall_enabled",
                   return_value=True):
            result = search_past_meetings(
                repo, query="budget", current_meeting_id="m_live",
            )
        assert result["ok"] is True
        assert repo.search_kwargs["exclude_meeting_id"] == "m_live"
        assert len(result["hits"]) == 1
        assert result["hits"][0]["meeting_id"] == "m_past"
        assert result["hits"][0]["ref"].startswith("past:m_past:")
        assert "sg_" not in result["text"]
        assert "sg_" not in result["hits"][0]["ref"]
        assert "sg_" not in result["hits"][0]["snippet"]

    def test_empty_query_asks_for_input(self):
        repo = _Repo(rows=[_hit()])
        with patch("services.settings.resolve_meeting_past_recall_enabled",
                   return_value=True):
            result = search_past_meetings(
                repo, query="   ", current_meeting_id="m_live",
            )
        assert result["ok"] is False
        assert result["hits"] == []
        assert repo.search_kwargs is None

    def test_clamps_limit_and_snippet(self):
        long_text = "budget " + ("word " * 80)
        repo = _Repo(rows=[
            _hit(f"m_{i}", long_text, title=f"Meet {i}")
            for i in range(30)
        ])
        with patch("services.settings.resolve_meeting_past_recall_enabled",
                   return_value=True):
            result = search_past_meetings(
                repo, query="budget", current_meeting_id="m_live", limit=99,
            )
        assert repo.search_kwargs["limit"] == MAX_HIT_LIMIT
        assert len(result["hits"]) <= MAX_HIT_LIMIT
        assert all(
            len(hit["snippet"]) <= MAX_SNIPPET_CHARS
            for hit in result["hits"]
        )

    def test_sanitizes_sg_ids_embedded_in_transcript_text(self):
        repo = _Repo(rows=[
            _hit(text="see sg_abcdef123456 from last week"),
        ])
        with patch("services.settings.resolve_meeting_past_recall_enabled",
                   return_value=True):
            result = search_past_meetings(
                repo, query="last", current_meeting_id="m_live",
            )
        assert "sg_" not in result["text"]
        assert "[id]" in result["text"]

class TestRecallSlice:
    def test_rejects_current_meeting_id(self):
        repo = _Repo(meetings={"m_live": {"id": "m_live", "title": "Now"}})
        with patch("services.settings.resolve_meeting_past_recall_enabled",
                   return_value=True):
            result = search_past_meetings(
                repo, query="", current_meeting_id="m_live",
                meeting_id="m_live",
            )
        assert result["ok"] is False
        assert "current meeting" in result["text"]

    def test_returns_opaque_slice(self):
        repo = _Repo(
            meetings={"m_past": {
                "id": "m_past",
                "title": "Prior sync",
                "started_at": "2026-02-02T09:00:00",
            }},
            segments={"m_past": [
                {"id": "sg_aaa111222333", "text": "Ship Friday",
                 "start_s": 3.5},
                {"id": "sg_bbb111222333", "text": "Agreed", "start_s": 8.0},
            ]},
        )
        with patch("services.settings.resolve_meeting_past_recall_enabled",
                   return_value=True):
            result = search_past_meetings(
                repo, current_meeting_id="m_live", meeting_id="m_past",
            )
        assert result["ok"] is True
        assert "Prior sync" in result["text"]
        assert "sg_" not in result["text"]
        assert result["hits"][0]["ref"] == "past:m_past:1"
        assert result["hits"][1]["ref"] == "past:m_past:2"
