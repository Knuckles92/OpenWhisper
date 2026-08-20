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
            assert SCHEMA_VERSION == 11
            with manager.engine.connect() as c:
                version = c.execute(
                    text("SELECT version FROM schema_version")).scalar()
            assert version == 11
            assert "meeting_sessions" in inspect(manager.engine).get_table_names()
        finally:
            manager.close()

    def test_agent_endpoint_json_roundtrip(self, repo):
        import json

        endpoint = {
            "profile_id": "custom_abcd1234",
            "name": "LM Studio",
            "kind": "custom",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key_env": "",
        }
        repo.create_meeting(
            id="m_endpoint", title="Endpoint meeting", status="active",
            started_at=datetime.now().isoformat(),
            host_token="host-token", guest_token="guest-token",
            cloud_enabled=True, spool_dir="/tmp/spool",
            agent_provider="custom_abcd1234", agent_model="local-qwen",
            agent_endpoint_json=json.dumps(endpoint),
        )
        meeting = repo.get_meeting("m_endpoint")
        stored = meeting["agent_endpoint_json"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored["base_url"] == "http://127.0.0.1:1234/v1"
        assert stored["profile_id"] == "custom_abcd1234"


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

    def test_terminal_meeting_with_unfinished_audio_requires_recovery(self, repo):
        make_meeting(repo)
        chunk_id = repo.register_chunk(
            meeting_id="m_test1", channel="mic", seq=0,
            file_path="/tmp/c0.wav", start_s=0.0, duration_s=20.0,
            sample_rate=16000,
        )
        repo.update_meeting("m_test1", status="ended")
        assert [m["id"] for m in repo.find_interrupted_meetings()] == ["m_test1"]
        repo.set_chunk_status(chunk_id, "done")
        assert repo.find_interrupted_meetings() == []

    def test_rename_updates_canonical_row_and_snapshot(self, repo):
        make_meeting(repo)
        repo.persist_state("m_test1", {
            "meeting_id": "m_test1", "seq": 2, "title": "Old",
            "status": "ended",
        })
        repo.rename_meeting("m_test1", "New title")
        meeting = repo.get_meeting("m_test1")
        assert meeting["title"] == "New title"
        assert json.loads(meeting["state_json"])["title"] == "New title"


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

    def test_atomic_commit_is_idempotent_and_done_cannot_regress(self, repo):
        make_meeting(repo)
        chunk_id = repo.register_chunk(
            meeting_id="m_test1", channel="mic", seq=0,
            file_path="/tmp/c0.wav", start_s=0.0, duration_s=20.0,
            sample_rate=16000,
        )
        segment = make_segment("m_test1", "sg_commit", 0.0, 2.0, "durable")
        segment.chunk_id = chunk_id
        rows, committed = repo.commit_chunk_transcription(
            "m_test1", chunk_id, [segment]
        )
        assert committed is True
        assert [row["id"] for row in rows] == ["sg_commit"]

        retry = make_segment("m_test1", "sg_other", 2.0, 4.0, "duplicate retry")
        retry.chunk_id = chunk_id
        rows, committed = repo.commit_chunk_transcription(
            "m_test1", chunk_id, [retry]
        )
        assert committed is False
        assert [row["id"] for row in rows] == ["sg_commit"]
        repo.set_chunk_status(chunk_id, "failed", error="late callback")
        assert repo.count_unfinished_chunks("m_test1") == 0


class TestSegments:
    def test_add_query_and_speaker_update(self, repo):
        make_meeting(repo)
        repo.add_segments([
            make_segment("m_test1", "sg_1", 0.0, 2.0, "first"),
            make_segment("m_test1", "sg_2", 2.0, 4.0, "second"),
            make_segment("m_test1", "sg_3", 4.0, 6.0, "third"),
        ])
        assert repo.segment_exists("m_test1", "sg_2")
        assert not repo.segment_exists("m_test1", "sg_nope")

        segments = repo.get_segments("m_test1")
        assert [s["id"] for s in segments] == ["sg_1", "sg_2", "sg_3"]

        after = repo.get_segments("m_test1", after_start_s=1.0)
        assert [s["id"] for s in after] == ["sg_2", "sg_3"]

        last2 = repo.get_last_segments("m_test1", 2)
        assert [s["id"] for s in last2] == ["sg_2", "sg_3"]

        repo.update_segment_speaker("m_test1", "sg_1", None, "human", True)
        seg = repo.get_segment("m_test1", "sg_1")
        assert seg["speaker_participant_id"] is None
        assert seg["speaker_pinned"] is True

    def test_embeddings_round_trip(self, repo):
        make_meeting(repo)
        repo.add_segments([make_segment("m_test1", "sg_e", 0.0, 2.0, "emb")])
        payload = b"\x00\x01\x02\x03"
        repo.set_segment_embedding("m_test1", "sg_e", payload)
        rows = repo.get_segment_embeddings("m_test1")
        assert len(rows) == 1
        assert rows[0]["embedding"] == payload

    def test_keyset_paging_handles_equal_timestamps(self, repo):
        make_meeting(repo)
        repo.add_segments([
            make_segment("m_test1", f"sg_{i}", 5.0, 6.0, str(i))
            for i in range(5)
        ])
        first = repo.get_segments_page("m_test1", limit=2)
        second = repo.get_segments_page(
            "m_test1", first[-1]["start_s"], first[-1]["id"], limit=2
        )
        third = repo.get_segments_page(
            "m_test1", second[-1]["start_s"], second[-1]["id"], limit=2
        )
        assert [row["id"] for row in first + second + third] == [
            "sg_0", "sg_1", "sg_2", "sg_3", "sg_4",
        ]

    def test_segment_reads_and_updates_are_meeting_scoped(self, repo):
        make_meeting(repo, "m_one")
        make_meeting(repo, "m_two")
        repo.add_segments([make_segment("m_one", "sg_one")])
        assert repo.get_segment("m_two", "sg_one") is None
        assert not repo.segment_exists("m_two", "sg_one")
        repo.update_segment_speaker("m_two", "sg_one", None, "human", True)
        assert repo.get_segment("m_one", "sg_one")["speaker_pinned"] is False


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

    def test_fts_search_excludes_meeting_and_honors_limit(self, repo):
        make_meeting(repo, "m_live")
        make_meeting(repo, "m_past")
        repo.add_segments([
            make_segment("m_live", "sg_live", 0.0, 2.0,
                         "the quarterly budget review"),
            make_segment("m_past", "sg_past_a", 0.0, 2.0,
                         "budget planning for next quarter"),
            make_segment("m_past", "sg_past_b", 2.0, 4.0,
                         "another budget follow-up"),
        ])
        hits = repo.search_transcripts(
            "budget", exclude_meeting_id="m_live", limit=1,
        )
        assert len(hits) == 1
        assert hits[0]["meeting_id"] == "m_past"
        assert all(hit["meeting_id"] != "m_live" for hit in hits)
        more = repo.search_transcripts("budget", exclude_meeting_id="m_live")
        assert len(more) == 2
        assert {hit["meeting_id"] for hit in more} == {"m_past"}


class TestWriteThrough:
    def test_ops_applied_mirrors_and_audit(self, repo, db):
        from meeting.state.schema import MeetingState
        from meeting.state.store import MeetingStateStore

        make_meeting(repo)
        repo.add_segments([
            make_segment("m_test1", "sg_evidence", text="supporting evidence")
        ])
        state = MeetingState(meeting_id="m_test1")
        store = MeetingStateStore(
            state, repository=repo,
            segment_exists=lambda segment_id: repo.segment_exists(
                "m_test1", segment_id
            ),
        )

        [added] = store.apply("agent", "agent", [
            {"op": "add_item", "card": "decisions", "text": "Ship it",
             "evidence": ["sg_evidence"]},
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

        listed = next(
            item for item in repo.list_events("m_test1")
            if item["seq"] == added.seq
        )
        assert listed["payload"]["text"] == "Ship it"

        meeting = repo.get_meeting("m_test1")
        snapshot = json.loads(meeting["state_json"])
        assert snapshot["seq"] == added.seq
        assert meeting["state_seq"] == added.seq

        [undone] = store.undo(added.seq, "p_host")
        assert undone.ok
        assert store.undo(added.seq, "p_host") == []
        events = repo.list_events("m_test1")
        original = next(event for event in events if event["seq"] == added.seq)
        undo_event = next(event for event in events if event["seq"] == undone.seq)
        assert original["undoable"] is False
        assert undo_event["undoable"] is True

    def test_question_and_participant_mirrors(self, repo, db):
        from meeting.state.schema import MeetingState
        from meeting.state.store import MeetingStateStore

        make_meeting(repo)
        store = MeetingStateStore(MeetingState(meeting_id="m_test1"),
                                  repository=repo)
        store.apply("agent", "agent", [
            {"op": "ask_question", "text": "Deadline confirmed?",
             "evidence": ["sg_anchor"]},
            {"op": "upsert_participant", "display_name": "Sam",
             "evidence": ["sg_anchor"]},
        ])
        from services.models import MeetingQuestion, MeetingParticipant
        with db.get_session() as session:
            assert session.query(MeetingQuestion).count() == 1
            participant = session.query(MeetingParticipant).one()
            assert participant.display_name == "Sam"
            assert participant.name_source == "agent_inferred"


class TestReplaceFinalTranscript:
    def test_keeps_pinned_speaker_and_remaps_evidence(self, repo):
        meeting_id = make_meeting(repo)
        live = [
            make_segment(meeting_id, "sg_old1", start=0.0, end=2.0, text="draft one"),
            make_segment(meeting_id, "sg_old2", start=2.0, end=4.0, text="draft two"),
        ]
        live[0].speaker_pinned = True
        live[0].speaker_participant_id = "p_me"
        live[0].speaker_source = "human"
        repo.add_segments(live)
        repo.update_meeting(meeting_id, state_json=json.dumps({
            "meeting_id": meeting_id,
            "cards": {
                "key_points": [{
                    "id": "it_human", "text": "kept", "status": "edited",
                    "pinned": False, "evidence": ["sg_old1"],
                }, {
                    "id": "it_agent", "text": "stale", "status": "proposed",
                    "evidence": ["sg_old2"],
                }],
                "user_notes": [], "decisions": [], "action_items": [],
                "risks": [], "timeline": [],
            },
            "questions": [],
            "rolling_summary_evidence": ["sg_old2"],
        }))
        cleaned = [
            TranscriptSegment(
                segment_id="sg_new1", meeting_id=meeting_id, chunk_id=None,
                channel="mic", start_s=0.1, end_s=2.1, text="clean one",
            ),
            TranscriptSegment(
                segment_id="sg_new2", meeting_id=meeting_id, chunk_id=None,
                channel="mic", start_s=2.1, end_s=4.1, text="clean two",
            ),
        ]
        rows, deleted, id_map = repo.replace_final_transcript(meeting_id, cleaned)
        ids = {row["id"] for row in rows}
        assert "sg_new1" in ids and "sg_new2" in ids
        assert "sg_old2" in deleted
        assert id_map["sg_old1"] == "sg_new1"
        pinned = next(row for row in rows if row["id"] == "sg_new1")
        assert pinned["speaker_pinned"] is True
        assert pinned["speaker_participant_id"] == "p_me"
        state = json.loads(repo.get_meeting(meeting_id)["state_json"])
        human = state["cards"]["key_points"][0]
        assert human["evidence"] == ["sg_new1"]
        # Proposed agent items are remapped too, so grounded live content can
        # survive a re-decode for the final consolidation to reconcile.
        agent_item = state["cards"]["key_points"][1]
        assert agent_item["evidence"] == ["sg_new2"]
        remaining_ids = {row["id"] for row in repo.get_segments(meeting_id)}
        assert remaining_ids == {"sg_new1", "sg_new2"}

    def test_mark_chunks_done(self, repo):
        meeting_id = make_meeting(repo)
        chunk_id = repo.register_chunk(
            meeting_id=meeting_id, channel="mic", seq=0,
            file_path="/tmp/a.wav", start_s=0.0, duration_s=1.0,
            sample_rate=16000, asr_status="pending",
        )
        assert repo.mark_chunks_done(meeting_id) == 1
        chunks = repo.get_audio_chunks(meeting_id)
        assert chunks[0]["id"] == chunk_id
        assert chunks[0]["asr_status"] == "done"
