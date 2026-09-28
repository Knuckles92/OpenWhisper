"""Fakes for the meeting web API: one active meeting behind an engine and repository."""

HOST_TOKEN = "host-secret-token-aaaaaaaaaaaaaaaa"
GUEST_TOKEN = "guest-secret-token-bbbbbbbbbbbbbbbb"


class FakeWebRepo:
    """The repository calls the meeting web API makes, over one active meeting."""

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


class FakeWebStore:
    """A state store with a fixed snapshot and a subscriber list."""

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


class FakeWebEngine:
    """A live meeting engine that records the lifecycle calls the API makes."""

    def __init__(self, meeting_id="m_test"):
        self.meeting_id = meeting_id
        self.store = FakeWebStore(meeting_id)
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
        self.cloud_calls = getattr(self, "cloud_calls", []) + [enabled]

    def get_transcript(self, after_start_s=-1.0, limit=None):
        return []

    def apply_client_action(self, actor_type, actor_id, op):
        return []

    def regenerate_tokens(self):
        self.tokens_regenerated = True
        return {"host_url": "/m/new-host", "guest_url": "/m/new-guest"}
