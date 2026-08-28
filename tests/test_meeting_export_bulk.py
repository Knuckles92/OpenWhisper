"""Tests for bulk Past Meetings export assembly."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from meeting.export.bulk import (
    FORMAT_JSON,
    FORMAT_MARKDOWN,
    FORMAT_TXT,
    collect_meeting_export,
    filter_export_meetings,
    list_export_meetings,
    meeting_file_stem,
    render_export_document,
    render_meeting_document,
    write_per_meeting_files,
)


def _row(meeting_id, *, status="ended", started="2026-03-15T14:30:00",
         title="Planning", has_transcript=True):
    return {
        "id": meeting_id,
        "title": title,
        "status": status,
        "started_at": started,
        "content_summary": {"has_transcript": has_transcript},
    }


def _entry(meeting_id="m_1", title="Planning"):
    meeting = {
        "id": meeting_id,
        "title": title,
        "started_at": "2026-03-15T14:30:00",
        "ended_at": "2026-03-15T15:00:00",
    }
    state = {"title": title, "participants": {}, "cards": {}}
    segments = [
        {"id": "sg_1", "start_s": 1.0, "end_s": 2.0, "text": "Hello",
         "channel": "mic"},
    ]
    return {"meeting": meeting, "state": state, "segments": segments}


def test_list_export_meetings_skips_live_and_fills_summary(monkeypatch):
    summarized = []

    class Repo:
        def list_meetings(self):
            return [
                {"id": "m_live", "status": "active"},
                {"id": "m_done", "status": "ended", "title": "Done"},
            ]

    def fake_summarize(repository, meeting_id):
        summarized.append(meeting_id)
        return {"has_transcript": False}

    monkeypatch.setattr(
        "meeting.content.summarize_meeting_content", fake_summarize
    )
    meetings = list_export_meetings(Repo())

    assert [row["id"] for row in meetings] == ["m_done"]
    assert meetings[0]["content_summary"] == {"has_transcript": False}
    assert summarized == ["m_done"]


def test_filter_export_meetings_applies_date_and_transcript():
    rows = [
        _row("m_old", started="2026-01-01T10:00:00"),
        _row("m_mid", started="2026-03-15T14:30:00"),
        _row("m_new", started="2026-06-01T09:00:00"),
        _row("m_silent", started="2026-03-20T09:00:00", has_transcript=False),
        _row("m_live", status="active", started="2026-03-15T14:30:00"),
        _row("m_undated", started=""),
    ]
    filtered = filter_export_meetings(
        rows,
        from_dt=datetime(2026, 3, 1),
        to_dt=datetime(2026, 3, 31, 23, 59, 59),
        only_with_transcript=True,
    )
    assert [row["id"] for row in filtered] == ["m_mid"]


def test_filter_keeps_unknown_transcript_when_only_with_transcript():
    row = _row("m_unknown")
    del row["content_summary"]
    filtered = filter_export_meetings(
        [row], only_with_transcript=True
    )
    assert [item["id"] for item in filtered] == ["m_unknown"]


def test_collect_meeting_export_parses_state_json():
    repo = SimpleNamespace(
        get_meeting=lambda meeting_id: {
            "id": meeting_id,
            "state_json": json.dumps({"title": "From JSON"}),
        },
        get_segments=lambda meeting_id: [{"text": "Hi"}],
    )
    entry = collect_meeting_export(repo, "m_1")
    assert entry["state"]["title"] == "From JSON"
    assert entry["segments"] == [{"text": "Hi"}]


def test_collect_meeting_export_missing_returns_none():
    repo = SimpleNamespace(get_meeting=lambda meeting_id: None)
    assert collect_meeting_export(repo, "m_gone") is None


def test_render_markdown_honors_content_toggles():
    entry = _entry()
    entry["state"]["cards"] = {
        "decisions": [
            {"id": "it_1", "card": "decisions", "text": "Ship it",
             "status": "confirmed", "data": {}, "pinned": False,
             "evidence": []},
        ]
    }
    full = render_meeting_document(entry, FORMAT_MARKDOWN)
    slim = render_meeting_document(
        entry,
        FORMAT_MARKDOWN,
        include_transcript=False,
        include_intelligence=False,
    )
    assert "Hello" in full
    assert "## Decisions" in full
    assert "Hello" not in slim
    assert "## Decisions" not in slim
    assert "Planning" in slim


def test_json_and_txt_ignore_content_toggles():
    entry = _entry()
    json_doc = json.loads(
        render_meeting_document(
            entry,
            FORMAT_JSON,
            include_transcript=False,
            include_intelligence=False,
        )
    )
    assert json_doc["segments"][0]["text"] == "Hello"
    txt = render_meeting_document(
        entry, FORMAT_TXT, include_transcript=False
    )
    assert "Hello" in txt


def test_combined_markdown_has_header(monkeypatch):
    monkeypatch.setattr(
        "meeting.export.bulk.datetime",
        SimpleNamespace(
            now=lambda: datetime(2026, 4, 1, 12, 0, 0),
        ),
    )
    document = render_export_document([_entry(), _entry("m_2", "Retro")], FORMAT_MARKDOWN)
    assert document.startswith("# Past Meetings Export\n")
    assert "2 meeting(s) · exported 2026-04-01 12:00" in document
    assert "Planning" in document
    assert "Retro" in document


def test_write_per_meeting_files_disambiguates(tmp_path):
    first = _entry("m_1", "Planning")
    second = _entry("m_2", "Planning")
    written = write_per_meeting_files(
        [first, second], FORMAT_TXT, str(tmp_path)
    )
    names = [path.split("\\")[-1].split("/")[-1] for path in written]
    assert names[0] != names[1]
    assert all(name.endswith(".txt") for name in names)
    assert (tmp_path / names[0]).read_text(encoding="utf-8")


def test_meeting_file_stem_is_filesystem_safe():
    meeting = {
        "title": "Q3 / Roadmap?",
        "started_at": "2026-03-15T14:30:00",
    }
    stem = meeting_file_stem(meeting, {"title": "Q3 / Roadmap?"})
    assert "/" not in stem
    assert "?" not in stem
    assert "20260315_143000" in stem
