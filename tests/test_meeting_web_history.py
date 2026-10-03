"""Bounded web history reads retain paging, content capabilities and host access."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from meeting.web.api import create_app
from meeting.web.ws import WsHub
from tests.fakes.meeting_web import FakeWebEngine
from tests.helpers import make_meeting, make_segment


@pytest.fixture
def history_client(repo):
    make_meeting(repo, "m_current")
    repo.update_meeting("m_current", started_at="2026-01-01T00:00:00Z")
    engine = FakeWebEngine("m_current")
    with TestClient(create_app(engine, repo, WsHub(engine, repo))) as client:
        yield client


def saved(repo, meeting_id, title="Saved", state=None):
    make_meeting(repo, meeting_id)
    repo.update_meeting(meeting_id, status="ended", title=title,
                        started_at="2026-09-01T00:00:00Z",
                        state_json=json.dumps(state or {}))


def forbidden(*args, **kwargs):
    raise AssertionError("History must not materialize complete audio/transcript lists")


def test_history_uses_bounded_aggregate_reads_and_stable_keyset_ties(history_client, repo, db, monkeypatch):
    for i in range(10):
        saved(repo, f"m_{i}")
        repo.add_segments([make_segment(f"m_{i}", f"s_{i}_{j}", start=j, text="Preview") for j in range(30)])
    for name in ("list_meetings", "get_segments", "get_audio_chunks"):
        monkeypatch.setattr(repo, name, forbidden)
    statements = []
    def observed(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(db.engine, "before_cursor_execute", observed)
    try:
        response = history_client.get("/api/meetings", params={"token": "host-token", "limit": 3})
    finally:
        event.remove(db.engine, "before_cursor_execute", observed)
    assert response.status_code == 200
    first = response.json()
    assert [row["id"] for row in first["meetings"]] == ["m_9", "m_8", "m_7"]
    assert len(statements) == 5  # auth, bounded sessions, audio, counts, first preview
    assert first["meetings"][0]["content_summary"]["transcript_segments"] == 30
    assert all("state_json" not in row and "host_token" not in row and "guest_token" not in row for row in first["meetings"])
    ids = [row["id"] for row in first["meetings"]]
    cursor = first["next_cursor"]
    while cursor:
        page = history_client.get("/api/meetings", params={"token": "host-token", "limit": 3, "cursor": cursor}).json()
        ids.extend(row["id"] for row in page["meetings"])
        cursor = page["next_cursor"]
    assert ids == [f"m_{i}" for i in range(9, -1, -1)] + ["m_current"]


def test_filtered_empty_window_keeps_a_continuation_and_query_is_literal(history_client, repo):
    saved(repo, "m_3")
    saved(repo, "m_2", "Literal 100%_exact", {"cards": {"decisions": [{"id": "d", "status": "confirmed"}]}})
    saved(repo, "m_1", "Literal 100ZZexact", {"cards": {"decisions": [{"id": "d", "status": "removed"}]}})
    first = history_client.get("/api/meetings", params={"token": "host-token", "limit": 1, "has_decisions": True}).json()
    assert first["meetings"] == [] and first["next_cursor"]
    second = history_client.get("/api/meetings", params={"token": "host-token", "limit": 1,
                                "has_decisions": True, "cursor": first["next_cursor"]}).json()
    assert [row["id"] for row in second["meetings"]] == ["m_2"]
    literal = history_client.get("/api/meetings", params={"token": "host-token", "q": "100%_"}).json()
    assert [row["id"] for row in literal["meetings"]] == ["m_2"]
    recent = history_client.get("/api/meetings", params={"token": "host-token", "since": "2026-10-01T00:00:00Z"}).json()
    assert recent["meetings"] == []


def test_preview_and_poll_do_not_query_transcript_pages(history_client, repo, monkeypatch):
    saved(repo, "m_saved")
    repo.add_segments([make_segment("m_saved", "s1")])
    for name in ("get_segments_page", "get_segments", "get_audio_chunks"):
        monkeypatch.setattr(repo, name, forbidden)
    preview = history_client.get("/api/meetings/m_saved", params={"token": "host-token", "include_transcript": False})
    assert preview.status_code == 200
    assert preview.json()["segments"] == [] and preview.json()["transcript_included"] is False
    assert preview.json()["meeting"]["has_transcript"] is True
    monkeypatch.setattr(repo, "get_meeting_content_summary", forbidden)
    state = history_client.get("/api/meetings/m_saved/state", params={"token": "host-token"})
    assert state.status_code == 200 and state.json()["state"]["meeting_id"] == "m_saved"


def test_history_paging_and_state_preserve_auth_and_bad_cursor_errors(history_client):
    for route in ("/api/meetings", "/api/meetings/m_current/state"):
        assert history_client.get(route, params={"token": "guest-token"}).status_code == 403
        assert history_client.get(route, params={"token": "wrong"}).status_code == 401
    assert history_client.get("/api/meetings", params={"token": "host-token", "cursor": "bad"}).status_code == 400
    assert history_client.get("/api/meetings/m_missing/state", params={"token": "host-token"}).status_code == 404
