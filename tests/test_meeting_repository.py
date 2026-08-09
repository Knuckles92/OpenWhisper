"""
Tests for Meeting Mode persistence: schema v10 tables, chunk lifecycle,
segments + FTS search, state write-through, and cascade-safe deletion.
"""
import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.interfaces import TranscriptSegment


@pytest.fixture
def db(tmp_path):
    from services.database import DatabaseManager
    manager = DatabaseManager(db_path=str(tmp_path / "test.db"))
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    from meeting.persist.repository import SqlMeetingRepository
    return SqlMeetingRepository(db=db)


def make_meeting(repo, meeting_id="m_test1"):
    repo.create_meeting(
        id=meeting_id, title="Test meeting", status="active",
        started_at=datetime.now().isoformat(),
        host_token="host-token", guest_token="guest-token",
        cloud_enabled=False, spool_dir="/tmp/spool",
    )
    return meeting_id


def make_segment(meeting_id, seg_id="sg_aaa111", start=1.0, end=3.0,
                 text="hello world", channel="mic"):
    return TranscriptSegment(
        segment_id=seg_id, meeting_id=meeting_id, chunk_id=None,
        channel=channel, start_s=start, end_s=end, text=text,
    )


class TestSchema:
    def test_v10_tables_exist(self, db):
        from sqlalchemy import inspect
        names = set(inspect(db.engine).get_table_names())
        expected = {"meeting_sessions", "meeting_audio_chunks",
                    "meeting_segments", "meeting_participants",
                    "meeting_state_items", "meeting_questions",
                    "meeting_events"}
        assert expected <= names
        # Legacy names must stay absent (they are dropped on startup)
        assert not {"meetings", "meeting_chunks", "meeting_insights"} & names

    def test_wal_mode_enabled(self, db):
        from sqlalchemy import text
        with db.engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert str(mode).lower() == "wal"

    def test_migration_from_v9(self, tmp_path):
        import sqlite3
        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (9)")
        conn.execute("""
            CREATE TABLE transcription_history (
                id TEXT PRIMARY KEY, text TEXT NOT NULL, raw_text TEXT,
                timestamp TEXT NOT NULL, model TEXT NOT NULL,
                audio_file TEXT, transcription_time REAL,
                audio_duration REAL, file_size INTEGER,
                cleanup_provider TEXT, cleanup_model TEXT
            )
        """)
        conn.commit()
        conn.close()

        from services.database import DatabaseManager, SCHEMA_VERSION
        manager = DatabaseManager(db_path=db_path)
        try:
            from sqlalchemy import inspect, text
            assert SCHEMA_VERSION == 10
            with manager.engine.connect() as c:
                version = c.execute(
                    text("SELECT version FROM schema_version")).scalar()
            assert version == 10
            assert "meeting_sessions" in inspect(manager.engine).get_table_names()
        finally:
            manager.close()


class TestMeetingLifecycle:
    def test_create_get_list_update(self, repo):
        make_meeting(repo)
        meeting = repo.get_meeting("m_test1")
        assert meeting["title"] == "Test meeting"
        assert meeting["status"] == "active"

        repo.update_meeting("m_test1", status="ended", title="Renamed")
        meeting = repo.get_meeting("m_test1")
        assert meeting["status"] == "ended"
        assert meeting["title"] == "Renamed"

        assert [m["id"] for m in repo.list_meetings()] == ["m_test1"]

    def test_heartbeat_and_interrupted(self, repo):
        make_meeting(repo)
        repo.heartbeat("m_test1")
        meeting = repo.get_meeting("m_test1")
        assert meeting["app_pid"] == os.getpid()
        assert meeting["app_heartbeat_at"]

        assert [m["id"] for m in repo.find_interrupted_meetings()] == ["m_test1"]
        repo.update_meeting("m_test1", status="ended")
        assert repo.find_interrupted_meetings() == []


class TestChunks:
    def test_chunk_lifecycle_and_retry_visibility(self, repo):
        make_meeting(repo)
        chunk_id = repo.register_chunk(
            meeting_id="m_test1", channel="mic", seq=0,
            file_path="/tmp/c0.wav", start_s=0.0, duration_s=20.0,
            sample_rate=16000,
        )
        assert isinstance(chunk_id, int)
        assert len(repo.get_pending_chunks("m_test1")) == 1

        repo.set_chunk_status(chunk_id, "processing")
        repo.set_chunk_status(chunk_id, "failed", error="boom")
        pending = repo.get_pending_chunks("m_test1")
        assert len(pending) == 1  # failed with attempts < 3 stays retryable
        assert pending[0]["asr_attempts"] == 1
        assert pending[0]["asr_error"] == "boom"

        for _ in range(2):
            repo.set_chunk_status(chunk_id, "processing")
            repo.set_chunk_status(chunk_id, "failed", error="boom")
        assert repo.get_pending_chunks("m_test1") == []  # 3 attempts exhausted

        repo.set_chunk_status(chunk_id, "done")
        assert repo.get_pending_chunks("m_test1") == []


class TestSegments:
    def test_add_query_and_speaker_update(self, repo):
        make_meeting(repo)
        repo.add_segments([
            make_segment("m_test1", "sg_1", 0.0, 2.0, "first"),
            make_segment("m_test1", "sg_2", 2.0, 4.0, "second"),
            make_segment("m_test1", "sg_3", 4.0, 6.0, "third"),
        ])
        assert repo.segment_exists("sg_2")
        assert not repo.segment_exists("sg_nope")

        segments = repo.get_segments("m_test1")
        assert [s["id"] for s in segments] == ["sg_1", "sg_2", "sg_3"]

        after = repo.get_segments("m_test1", after_start_s=1.0)
        assert [s["id"] for s in after] == ["sg_2", "sg_3"]

        last2 = repo.get_last_segments("m_test1", 2)
        assert [s["id"] for s in last2] == ["sg_2", "sg_3"]

        repo.update_segment_speaker("sg_1", "p_x", "human", True)
        seg = repo.get_segment("sg_1")
        assert seg["speaker_participant_id"] == "p_x"
        assert seg["speaker_pinned"] is True

    def test_embeddings_round_trip(self, repo):
        make_meeting(repo)
        repo.add_segments([make_segment("m_test1", "sg_e", 0.0, 2.0, "emb")])
        payload = b"\x00\x01\x02\x03"
        repo.set_segment_embedding("sg_e", payload)
        rows = repo.get_segment_embeddings("m_test1")
        assert len(rows) == 1
        assert rows[0]["embedding"] == payload


class TestSearchAndDelete:
    def test_fts_search_and_orphan_free_delete(self, repo):
        make_meeting(repo)
        repo.add_segments([
            make_segment("m_test1", "sg_q", 0.0, 2.0,
                         "the quarterly budget review"),
        ])
        hits = repo.search_transcripts("budget")
        assert len(hits) == 1
        assert hits[0]["segment_id"] == "sg_q"
        assert hits[0]["title"] == "Test meeting"

        # FTS operators from user input must not break the query
        assert repo.search_transcripts('budget" OR 1=1 NEAR(') == []

        repo.delete_meeting("m_test1")
        assert repo.get_meeting("m_test1") is None
        assert repo.get_segments("m_test1") == []
        # The FTS index must not serve orphaned rows after deletion
        assert repo.search_transcripts("budget") == []


class TestWriteThrough:
    def test_ops_applied_mirrors_and_audit(self, repo, db):
        from meeting.state.schema import MeetingState
        from meeting.state.store import MeetingStateStore

        make_meeting(repo)
        state = MeetingState(meeting_id="m_test1")
        store = MeetingStateStore(state, repository=repo,
                                  segment_exists=repo.segment_exists)

        [added] = store.apply("agent", "agent", [
            {"op": "add_item", "card": "decisions", "text": "Ship it"},
        ])
        assert added.ok

        from services.models import MeetingStateItem, MeetingEvent
        with db.get_session() as session:
            items = session.query(MeetingStateItem).all()
            assert len(items) == 1
            assert items[0].text == "Ship it"
            assert items[0].card == "decisions"
            events = session.query(MeetingEvent).all()
            assert len(events) == 1
            assert events[0].seq == added.seq
            assert events[0].actor_type == "agent"

        event = repo.get_event("m_test1", added.seq)
        assert event["inverse"] == {"op": "remove_item", "id": added.target_id}

        meeting = repo.get_meeting("m_test1")
        snapshot = json.loads(meeting["state_json"])
        assert snapshot["seq"] == added.seq
        assert meeting["state_seq"] == added.seq

    def test_question_and_participant_mirrors(self, repo, db):
        from meeting.state.schema import MeetingState
        from meeting.state.store import MeetingStateStore

        make_meeting(repo)
        store = MeetingStateStore(MeetingState(meeting_id="m_test1"),
                                  repository=repo)
        store.apply("agent", "agent", [
            {"op": "ask_question", "text": "Deadline confirmed?"},
            {"op": "upsert_participant", "display_name": "Sam"},
        ])
        from services.models import MeetingQuestion, MeetingParticipant
        with db.get_session() as session:
            assert session.query(MeetingQuestion).count() == 1
            participant = session.query(MeetingParticipant).one()
            assert participant.display_name == "Sam"
            assert participant.name_source == "agent_inferred"
