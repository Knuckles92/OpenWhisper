"""
Tests for meeting web auth: role resolve, host-only ops, token compare,
export token stripping, and the re-run-insights in-flight guard.
"""
import threading

import pytest

from meeting.web.auth import (
    ROLE_GUEST,
    ROLE_HOST,
    generate_token,
    generate_token_pair,
    resolve_role,
)
from meeting.web.ws import WsHub

HOST_TOKEN = "host-secret-token-aaaaaaaaaaaaaaaa"
GUEST_TOKEN = "guest-secret-token-bbbbbbbbbbbbbbbb"

class FakeRepo:
    def __init__(self, meeting=None, meetings=None):
        self._meeting = meeting or {
            "id": "m_test",
            "title": "Auth Test",
            "status": "active",
            "started_at": "2026-01-01T00:00:00",
            "ended_at": None,
            "paused_total_s": 0.0,
            "cloud_enabled": False,
            "asr_model": "base",
            "host_token": HOST_TOKEN,
            "guest_token": GUEST_TOKEN,
            "state_json": "{}",
        }
        self._meetings = meetings if meetings is not None else [self._meeting]
        self.deleted = []
        self.search_calls = []
        self._segments = []
        self._chunks = [{
            "id": 1,
            "meeting_id": "m_test",
            "channel": "loopback",
            "duration_s": 1.0,
            "file_path": "/tmp/loopback.wav",
        }]

    def get_meeting(self, meeting_id):
        if self._meeting and self._meeting["id"] == meeting_id:
            return dict(self._meeting)
        return None

    def list_meetings(self):
        return [dict(m) for m in self._meetings]

    def delete_meeting(self, meeting_id):
        self.deleted.append(meeting_id)

    def search_transcripts(self, q, *, exclude_meeting_id=None, limit=200):
        self.search_calls.append(q)
        return []

    def get_segments(self, meeting_id, after_start_s=-1.0, limit=None):
        rows = [
            dict(row) for row in self._segments
            if row["meeting_id"] == meeting_id
            and float(row["start_s"]) > float(after_start_s)
        ]
        return rows[:limit] if limit else rows

    def get_audio_chunks(self, meeting_id):
        return [
            dict(row) for row in self._chunks
            if row["meeting_id"] == meeting_id
        ]

    def get_segments_page(self, meeting_id, cursor_start_s=None,
                          cursor_id=None, limit=500):
        rows = sorted(
            (dict(row) for row in self._segments
             if row["meeting_id"] == meeting_id),
            key=lambda row: (row["start_s"], row["id"]),
        )
        if cursor_start_s is not None and cursor_id is not None:
            rows = [
                row for row in rows
                if (row["start_s"], row["id"])
                > (float(cursor_start_s), cursor_id)
            ]
        return rows[:limit]

    def get_segment(self, meeting_id, segment_id):
        return next((dict(row) for row in self._segments
                     if row["meeting_id"] == meeting_id
                     and row["id"] == segment_id), None)

    def get_last_segments(self, meeting_id, n):
        return []

    def update_meeting(self, meeting_id, **fields):
        self._meeting.update(fields)

    def list_events(self, meeting_id, before_seq=None, limit=100):
        return [{"seq": 2, "ts": "2026-01-01T00:00:01",
                 "actor_type": "agent", "actor_id": "agent",
                 "action": "add_item", "target_id": "it_1",
                 "undoable": True}]

class FakeStore:
    def __init__(self, meeting_id="m_test"):
        self._subs = []
        self._state = {
            "meeting_id": meeting_id, "seq": 0, "title": "Auth Test",
            "status": "active", "cards": {}, "participants": {},
            "questions": [], "topic": {}, "rolling_summary": "",
            "cloud_enabled": False, "intelligence_online": True,
        }

    def snapshot(self):
        return dict(self._state)

    def subscribe(self, cb):
        self._subs.append(cb)

    def unsubscribe(self, cb):
        if cb in self._subs:
            self._subs.remove(cb)

class FakeEngine:
    def __init__(self, meeting_id="m_test"):
        self.meeting_id = meeting_id
        self.store = FakeStore(meeting_id)
        self.ended = False
        self.paused = False
        self.resumed = False
        self.tokens_regenerated = False

    def is_active(self):
        return not self.ended

    def end(self):
        self.ended = True

    def pause(self):
        self.paused = True

    def resume(self):
        self.resumed = True

    def set_cloud_enabled(self, enabled):
        pass

    def get_transcript(self, after_start_s=-1.0, limit=None):
        return []

    def apply_client_action(self, actor_type, actor_id, op):
        return []

    def regenerate_tokens(self):
        self.tokens_regenerated = True
        return {"host_url": "/m/new-host", "guest_url": "/m/new-guest"}

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from meeting.web.api import create_app

    engine = FakeEngine()
    repo = FakeRepo()
    hub = WsHub(engine, repo)
    app = create_app(engine, repo, hub)
    with TestClient(app) as tc:
        yield tc, engine, repo

class TestResolveRole:
    def test_host_and_guest_match(self):
        assert resolve_role(HOST_TOKEN, HOST_TOKEN, GUEST_TOKEN) == ROLE_HOST
        assert resolve_role(GUEST_TOKEN, HOST_TOKEN, GUEST_TOKEN) == ROLE_GUEST

    def test_wrong_token_rejected(self):
        assert resolve_role("nope", HOST_TOKEN, GUEST_TOKEN) is None
        assert resolve_role("", HOST_TOKEN, GUEST_TOKEN) is None
        assert resolve_role(None, HOST_TOKEN, GUEST_TOKEN) is None

    def test_host_preferred_when_tokens_collide(self):
        # Same string presented: host checked first
        assert resolve_role("same", "same", "same") == ROLE_HOST

    def test_generate_tokens_are_distinct(self):
        a, b = generate_token_pair()
        assert a != b
        assert len(generate_token()) >= 32

class TestHostOnlyAuthz:
    def test_guest_cannot_list_meetings(self, client):
        tc, _, _ = client
        r = tc.get("/api/meetings", params={"token": GUEST_TOKEN})
        assert r.status_code == 403
        assert r.json()["detail"] == "host only"

    def test_host_can_list_meetings(self, client):
        tc, _, _ = client
        r = tc.get("/api/meetings", params={"token": HOST_TOKEN})
        assert r.status_code == 200
        assert r.json()["meetings"][0]["id"] == "m_test"
        assert r.json()["meetings"][0]["display_title"] == "Auth Test"
        assert r.json()["meetings"][0]["has_audio"] is True
        assert r.json()["meetings"][0]["can_rerun_speakers"] is True
        # Tokens must not leak in public meeting payloads
        assert "host_token" not in r.json()["meetings"][0]
        assert "guest_token" not in r.json()["meetings"][0]

    def test_meeting_list_uses_topic_when_no_title_was_set(self, client):
        tc, _, repo = client
        repo._meetings = [{
            "id": "m_history",
            "title": "",
            "status": "ended",
            "started_at": "2026-01-02T00:00:00",
            "state_json": '{"topic":{"current":"Quarterly roadmap"}}',
        }]

        response = tc.get("/api/meetings", params={"token": HOST_TOKEN})

        assert response.status_code == 200
        meeting = response.json()["meetings"][0]
        assert meeting["title"] == ""
        assert meeting["display_title"] == "Quarterly roadmap"
        assert "state_json" not in meeting
        assert "finalization_status" in meeting
        assert "finalization_deferred" in meeting

    def test_meeting_list_exposes_compact_finalization_fields(self, client):
        tc, _, repo = client
        repo._meetings = [{
            "id": "m_saved",
            "title": "Saved meeting",
            "status": "ended",
            "started_at": "2026-01-02T00:00:00",
            "cloud_enabled": True,
            "state_json": (
                '{"meeting_id":"m_saved","status":"ended","cloud_enabled":true,'
                '"finalization":{"status":"failed","message":"interrupted",'
                '"card_deferred":true}}'
            ),
        }]

        response = tc.get("/api/meetings", params={"token": HOST_TOKEN})

        assert response.status_code == 200
        meeting = response.json()["meetings"][0]
        assert meeting["finalization_status"] == "failed"
        assert meeting["finalization_deferred"] is True
        assert meeting["insights_pill"] == "Saved for later"
        assert meeting["insights_tone"] == "warning"
        assert "state_json" not in meeting
        assert "steps" not in meeting
        assert "host_token" not in meeting

    def test_guest_cannot_end_meeting(self, client):
        tc, engine, _ = client
        r = tc.post("/api/meeting/end", params={"token": GUEST_TOKEN})
        assert r.status_code == 403
        assert engine.ended is False

    def test_host_can_end_meeting(self, client):
        tc, engine, _ = client
        r = tc.post("/api/meeting/end", params={"token": HOST_TOKEN})
        assert r.status_code == 200
        assert engine.ended is True

    def test_guest_cannot_search_or_export(self, client):
        tc, _, _ = client
        assert tc.get("/api/search", params={"token": GUEST_TOKEN, "q": "x"}
                      ).status_code == 403
        assert tc.get("/api/export/md", params={"token": GUEST_TOKEN}
                      ).status_code == 403

    def test_host_can_search(self, client):
        tc, _, repo = client
        r = tc.get("/api/search", params={"token": HOST_TOKEN, "q": "budget"})
        assert r.status_code == 200
        assert repo.search_calls == ["budget"]

    def test_invalid_token_is_401(self, client):
        tc, _, _ = client
        r = tc.get("/api/session", params={"token": "wrong"})
        assert r.status_code == 401

    def test_guest_can_read_session(self, client):
        tc, _, _ = client
        r = tc.get("/api/session", params={"token": GUEST_TOKEN})
        assert r.status_code == 200
        assert r.json()["role"] == "guest"
        assert "host_token" not in r.json()["meeting"]

    def test_transcript_keyset_pagination_has_no_gaps(self, client):
        tc, _, repo = client
        repo._segments = [
            {"id": f"sg_{i}", "meeting_id": "m_test", "start_s": 1.0,
             "end_s": 2.0, "text": str(i), "channel": "mic"}
            for i in range(3)
        ]
        first = tc.get("/api/transcript", params={
            "token": HOST_TOKEN, "limit": 2,
        })
        assert first.status_code == 200
        assert [row["id"] for row in first.json()["items"]] == ["sg_0", "sg_1"]
        cursor = first.json()["next_cursor"]
        assert cursor
        second = tc.get("/api/transcript", params={
            "token": HOST_TOKEN, "limit": 2, "cursor": cursor,
        })
        assert [row["id"] for row in second.json()["items"]] == ["sg_2"]
        assert second.json()["next_cursor"] is None

    def test_guest_segment_access_is_scoped_to_current_meeting(self, client):
        tc, _, repo = client
        repo._segments = [{
            "id": "sg_1", "meeting_id": "m_test", "start_s": 1.0,
            "end_s": 2.0, "text": "hello", "channel": "mic",
        }]
        allowed = tc.get("/api/meetings/m_test/segments/sg_1",
                         params={"token": GUEST_TOKEN})
        assert allowed.status_code == 200
        blocked = tc.get("/api/meetings/m_other/segments/sg_1",
                         params={"token": GUEST_TOKEN})
        assert blocked.status_code == 403

    def test_historical_state_is_upgraded_to_current_wire_shape(self, client):
        tc, engine, _ = client
        engine.store = None  # force the persisted-state branch
        response = tc.get("/api/meetings/m_test", params={"token": HOST_TOKEN})
        assert response.status_code == 200
        state = response.json()["state"]
        assert state["meeting_id"] == "m_test"
        assert state["rolling_summary_evidence"] == []
        assert state["capture"] == {
            "mic_available": False,
            "loopback_available": False,
            "message": "",
        }

    def test_activity_and_token_rotation_are_host_only(self, client):
        tc, engine, _ = client
        assert tc.get("/api/events", params={"token": GUEST_TOKEN}).status_code == 403
        events = tc.get("/api/events", params={"token": HOST_TOKEN})
        assert events.status_code == 200
        assert events.json()["events"][0]["undoable"] is True
        assert tc.post("/api/meeting/tokens/regenerate",
                       params={"token": GUEST_TOKEN}).status_code == 403
        rotated = tc.post("/api/meeting/tokens/regenerate",
                          params={"token": HOST_TOKEN})
        assert rotated.status_code == 200
        assert rotated.json()["host_url"] == "/m/new-host"
        assert engine.tokens_regenerated is True

    def test_audio_allows_current_guest_but_not_other_meetings(
        self, client, monkeypatch, tmp_path
    ):
        tc, _, _ = client
        audio = tmp_path / "playback.wav"
        audio.write_bytes(b"RIFF-test")
        monkeypatch.setattr("meeting.web.api.build_playback",
                            lambda repository, meeting_id: str(audio))
        allowed = tc.get("/api/meetings/m_test/audio",
                         params={"token": GUEST_TOKEN})
        assert allowed.status_code == 200
        assert allowed.headers["cache-control"] == "no-store"
        partial = tc.get(
            "/api/meetings/m_test/audio",
            params={"token": HOST_TOKEN},
            headers={"Range": "bytes=0-3"},
        )
        assert partial.status_code == 206
        assert partial.content == b"RIFF"
        blocked = tc.get("/api/meetings/m_other/audio",
                         params={"token": GUEST_TOKEN})
        assert blocked.status_code == 403

class TestRerunSpeakers:
    def test_guest_cannot_rerun(self, client):
        tc, _, _ = client
        r = tc.post("/api/meetings/m_test/respeakers", params={"token": GUEST_TOKEN})
        assert r.status_code == 403

    def test_active_meeting_conflicts(self, client):
        tc, _, _ = client
        r = tc.post("/api/meetings/m_test/respeakers", params={"token": HOST_TOKEN})
        assert r.status_code == 409

    def _ended(self, client):
        tc, engine, _ = client
        engine.ended = True
        return tc

    def test_on_device_backend_is_400(self, client, monkeypatch):
        tc = self._ended(client)
        monkeypatch.setattr(
            "services.settings.resolve_meeting_speaker_id_backend",
            lambda settings=None: "local",
        )
        r = tc.post("/api/meetings/m_test/respeakers", params={"token": HOST_TOKEN})
        assert r.status_code == 400
        assert r.json()["detail"] == "speaker identification is set to on-device"

    def test_missing_audio_consent_is_400(self, client, monkeypatch):
        tc = self._ended(client)
        monkeypatch.setattr(
            "services.settings.resolve_meeting_speaker_id_backend",
            lambda settings=None: "openai",
        )
        monkeypatch.setattr(
            "services.settings.resolve_meeting_audio_upload_consent",
            lambda settings=None: False,
        )
        r = tc.post("/api/meetings/m_test/respeakers", params={"token": HOST_TOKEN})
        assert r.status_code == 400
        assert r.json()["detail"] == "audio-upload consent has not been given"

    def test_missing_openai_key_is_400(self, client, monkeypatch):
        tc = self._ended(client)
        monkeypatch.setattr(
            "services.settings.resolve_meeting_speaker_id_backend",
            lambda settings=None: "openai",
        )
        monkeypatch.setattr(
            "services.settings.resolve_meeting_audio_upload_consent",
            lambda settings=None: True,
        )
        monkeypatch.setattr(
            "services.transcript_cleanup.find_api_key",
            lambda provider: "",
        )
        r = tc.post("/api/meetings/m_test/respeakers", params={"token": HOST_TOKEN})
        assert r.status_code == 400
        assert r.json()["detail"] == "no OpenAI API key is configured"

    def test_unknown_meeting_is_404(self, client):
        tc = self._ended(client)
        r = tc.post("/api/meetings/m_gone/respeakers", params={"token": HOST_TOKEN})
        assert r.status_code == 404

    def test_meeting_without_system_audio_is_400(self, client):
        tc, engine, repo = client
        engine.ended = True
        repo._chunks = []

        r = tc.post(
            "/api/meetings/m_test/respeakers",
            params={"token": HOST_TOKEN},
        )

        assert r.status_code == 400
        assert "no system-audio recording" in r.json()["detail"]

    def test_host_rerun_calls_respeakers(self, client, monkeypatch):
        tc, engine, repo = client
        engine.ended = True
        monkeypatch.setattr(
            "services.settings.resolve_meeting_speaker_id_backend",
            lambda settings=None: "openai",
        )
        monkeypatch.setattr(
            "services.settings.resolve_meeting_audio_upload_consent",
            lambda settings=None: True,
        )
        monkeypatch.setattr(
            "services.transcript_cleanup.find_api_key",
            lambda provider: "sk-test",
        )
        calls = {}

        def fake_rerun(repository, meeting_id, **kwargs):
            calls["meeting_id"] = meeting_id
            calls["repository"] = repository
            calls.update(kwargs)
            return {"ok": True, "relabeled": 3}

        monkeypatch.setattr("meeting.web.api.rerun_speakers", fake_rerun)
        r = tc.post("/api/meetings/m_test/respeakers", params={"token": HOST_TOKEN})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "relabeled": 3}
        assert calls["meeting_id"] == "m_test"
        assert calls["repository"] is repo
        assert calls["api_key"] == "sk-test"
        assert calls["spool_dir"] == repo._meeting.get("spool_dir")

class TestRerunInsights:
    def test_guest_cannot_rerun(self, client):
        tc, _, _ = client
        r = tc.post("/api/meetings/m_test/reinsights", params={"token": GUEST_TOKEN})
        assert r.status_code == 403

    def test_active_meeting_conflicts(self, client):
        tc, _, _ = client
        r = tc.post("/api/meetings/m_test/reinsights", params={"token": HOST_TOKEN})
        assert r.status_code == 409

    def test_unknown_meeting_is_404(self, client):
        tc, engine, _ = client
        engine.ended = True
        r = tc.post("/api/meetings/m_gone/reinsights", params={"token": HOST_TOKEN})
        assert r.status_code == 404

    def test_host_rerun_returns_result(self, client, monkeypatch):
        tc, engine, _ = client
        engine.ended = True
        calls = {}

        def fake_rerun(repository, meeting_id, **kwargs):
            calls["meeting_id"] = meeting_id
            calls.update(kwargs)
            return {"ok": True, "state": {"seq": 3}, "applied": 2, "error": None}

        monkeypatch.setattr("meeting.web.api.rerun_finalization", fake_rerun)
        r = tc.post("/api/meetings/m_test/reinsights", params={"token": HOST_TOKEN})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "state": {"seq": 3}, "applied": 2,
                            "error": None}
        assert calls["meeting_id"] == "m_test"
        assert calls["provider"] == "openrouter"  # fallback when unrecorded
        assert calls["agent_core_kind"] == "pi"

    def test_missing_transcript_is_400(self, client, monkeypatch):
        tc, engine, _ = client
        engine.ended = True

        def fake_rerun(repository, meeting_id, **kwargs):
            raise ValueError("meeting has no transcript")

        monkeypatch.setattr("meeting.web.api.rerun_finalization", fake_rerun)
        r = tc.post("/api/meetings/m_test/reinsights", params={"token": HOST_TOKEN})
        assert r.status_code == 400
        assert r.json()["detail"] == "meeting has no transcript"

    def test_second_rerun_while_running_is_409(self, client, monkeypatch):
        """A double-click must not start two agent cores on one meeting."""
        tc, engine, _ = client
        engine.ended = True
        running = threading.Event()
        release = threading.Event()

        def fake_rerun(repository, meeting_id, **kwargs):
            running.set()
            release.wait(timeout=10)
            return {"ok": True, "state": {"seq": 1}, "applied": 0, "error": None}

        monkeypatch.setattr("meeting.web.api.rerun_finalization", fake_rerun)
        first: dict = {}

        def run_first():
            first["response"] = tc.post("/api/meetings/m_test/reinsights",
                                        params={"token": HOST_TOKEN})

        worker = threading.Thread(target=run_first, daemon=True)
        worker.start()
        assert running.wait(timeout=10)

        second = tc.post("/api/meetings/m_test/reinsights",
                         params={"token": HOST_TOKEN})
        assert second.status_code == 409

        release.set()
        worker.join(timeout=10)
        assert first["response"].status_code == 200

        # The guard releases: a later run is accepted again.
        third = tc.post("/api/meetings/m_test/reinsights",
                        params={"token": HOST_TOKEN})
        assert third.status_code == 200

class TestExportTokenStripping:
    @pytest.mark.parametrize("fmt", ["md", "json", "txt"])
    def test_export_never_carries_capability_tokens(self, client, fmt):
        tc, _, _ = client
        r = tc.get(f"/api/export/{fmt}", params={"token": HOST_TOKEN})
        assert r.status_code == 200
        body = r.text
        assert HOST_TOKEN not in body
        assert GUEST_TOKEN not in body
        assert "host_token" not in body
        assert "guest_token" not in body

    def test_unknown_export_format_is_404(self, client):
        tc, _, _ = client
        r = tc.get("/api/export/pdf", params={"token": HOST_TOKEN})
        assert r.status_code == 404
