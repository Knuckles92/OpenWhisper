"""Durable insertion-order delivery to the meeting agent."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from meeting.agent.scheduler import CheckpointScheduler
from meeting.persist.repository import SqlMeetingRepository
from services.database import DatabaseManager
from tests.fakes.scheduler import FakeClock, FakeNotesAgent, FakeSchedulerStore
from tests.helpers import make_meeting, make_segment


class _RepositoryEngine:
    def __init__(self, repo: SqlMeetingRepository, meeting_id: str):
        self.repository = repo
        self.meeting_id = meeting_id
        self.store = FakeSchedulerStore()
        self.clock = FakeClock(700)

    def get_transcript(self, after_start_s=-1.0, limit=None):
        return self.repository.get_segments(
            self.meeting_id, after_start_s=after_start_s, limit=limit,
        )

    def get_transcript_delivery_page(self, after_seq=0, limit=300):
        return self.repository.get_segments_delivery_page(
            self.meeting_id, after_seq=after_seq, limit=limit,
        )

    def get_transcript_delivery_cursor(self, consumer):
        return self.repository.get_delivery_cursor(self.meeting_id, consumer)

    def advance_transcript_delivery_cursor(self, consumer, seq):
        self.repository.advance_delivery_cursor(self.meeting_id, consumer, seq)

    def get_transcript_delivery_high_water(self):
        return self.repository.get_delivery_high_water(self.meeting_id)


def _passes(agent, *, notes=False):
    return [p for p in agent.calls if p.is_notes is notes and not p.is_polish]


def test_delayed_old_start_segment_arrives_after_cursor_and_survives_reopen(repo, db):
    meeting_id = make_meeting(repo)
    repo.add_segments([make_segment(meeting_id, "newer", start=600)])
    engine = _RepositoryEngine(repo, meeting_id)
    agent = FakeNotesAgent()
    scheduler = CheckpointScheduler(engine, agent)
    scheduler._fire()
    assert [s["id"] for s in _passes(agent)[-1].new_segments] == ["newer"]

    # Its speech time is over 180 seconds behind the latest sent segment.
    repo.add_segments([make_segment(meeting_id, "late", start=1)])
    scheduler._fire()
    assert [s["id"] for s in _passes(agent)[-1].new_segments] == ["late"]
    scheduler._successful_checkpoints += 2
    scheduler._maybe_fire_notes()
    assert [s["id"] for s in _passes(agent, notes=True)[-1].new_segments] == ["late"]

    reopened = DatabaseManager(db_path=db.db_path)
    try:
        durable = SqlMeetingRepository(db=reopened)
        assert durable.get_delivery_cursor(meeting_id, "cards") == 2
        assert durable.get_delivery_cursor(meeting_id, "notes") == 2
    finally:
        reopened.close()

    # A speaker update does not create a new delivery event, and a new
    # scheduler resumes from the durable acknowledgment after reconnect.
    repo.update_segment_speaker(meeting_id, "late", None, "human", True)
    fresh_agent = FakeNotesAgent()
    fresh = CheckpointScheduler(engine, fresh_agent)
    fresh._fire()
    assert _passes(fresh_agent) == []
    assert repo.get_segments_delivery_page(meeting_id, after_seq=2) == []


def test_delivery_pages_are_bounded_and_failed_pass_is_replayed(repo):
    meeting_id = make_meeting(repo)
    repo.add_segments([
        make_segment(meeting_id, f"sg_{index}", start=float(index))
        for index in range(625)
    ])
    engine = _RepositoryEngine(repo, meeting_id)
    agent = FakeNotesAgent(fail_times=1)
    scheduler = CheckpointScheduler(engine, agent)

    scheduler._fire()
    assert repo.get_delivery_cursor(meeting_id, "cards") == 0
    scheduler._fire()
    assert [len(p.new_segments) for p in _passes(agent)] == [300, 300]
    assert repo.get_delivery_cursor(meeting_id, "cards") == 300
    scheduler._fire()
    scheduler._fire()
    assert [len(p.new_segments) for p in _passes(agent)] == [300, 300, 300, 25]
    assert repo.get_delivery_cursor(meeting_id, "cards") == 625
    assert repo.get_segments_delivery_page(meeting_id, after_seq=625) == []


def test_delivery_sequence_does_not_reuse_deleted_highest_row(repo, db):
    from sqlalchemy import text

    meeting_id = make_meeting(repo)
    repo.add_segments([make_segment(meeting_id, "first", start=500)])
    first = repo.get_segments_delivery_page(meeting_id)[0]["delivery_seq"]
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM meeting_segments WHERE id='first'"))
    repo.add_segments([make_segment(meeting_id, "late", start=1)])
    page = repo.get_segments_delivery_page(meeting_id, after_seq=first)
    assert [row["id"] for row in page] == ["late"]
    assert page[0]["delivery_seq"] > first


def test_deleted_segment_redecoded_with_same_id_gets_new_delivery(repo, db):
    from sqlalchemy import text

    meeting_id = make_meeting(repo)
    repo.add_segments([make_segment(meeting_id, "redecoded", start=500)])
    first = repo.get_segments_delivery_page(meeting_id)[0]["delivery_seq"]
    repo.advance_delivery_cursor(meeting_id, "cards", first)
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM meeting_segments WHERE id='redecoded'"))
    repo.add_segments([make_segment(meeting_id, "redecoded", start=1)])
    page = repo.get_segments_delivery_page(
        meeting_id, after_seq=repo.get_delivery_cursor(meeting_id, "cards"),
    )
    assert [row["id"] for row in page] == ["redecoded"]
    assert page[0]["delivery_seq"] > first


def test_concurrent_cursor_advancement_is_atomic_and_monotonic(repo):
    meeting_id = make_meeting(repo)
    barrier = threading.Barrier(8)
    sequence = [8, 2, 6, 1, 7, 3, 5, 4]

    def advance(value):
        barrier.wait(timeout=5)
        repo.advance_delivery_cursor(meeting_id, "cards", value)

    with ThreadPoolExecutor(max_workers=8) as workers:
        for future in [workers.submit(advance, value) for value in sequence]:
            future.result(timeout=10)
    assert repo.get_delivery_cursor(meeting_id, "cards") == 8
    repo.advance_delivery_cursor(meeting_id, "cards", 1)
    assert repo.get_delivery_cursor(meeting_id, "cards") == 8


def test_failed_backlog_read_does_not_start_scheduler(repo):
    meeting_id = make_meeting(repo)
    engine = _RepositoryEngine(repo, meeting_id)

    def fail_high_water():
        raise RuntimeError("database unavailable")

    engine.get_transcript_delivery_high_water = fail_high_water
    scheduler = CheckpointScheduler(engine, FakeNotesAgent())
    with pytest.raises(RuntimeError, match="database unavailable"):
        scheduler.start()
    assert scheduler._thread is None


def test_restart_drains_notes_backlog_when_cards_are_caught_up(repo, monkeypatch):
    meeting_id = make_meeting(repo)
    repo.add_segments([
        make_segment(meeting_id, "older", start=600),
        make_segment(meeting_id, "late", start=1),
    ])
    high_water = repo.get_delivery_high_water(meeting_id)
    repo.advance_delivery_cursor(meeting_id, "cards", high_water)
    engine = _RepositoryEngine(repo, meeting_id)
    agent = FakeNotesAgent()
    scheduler = CheckpointScheduler(engine, agent)
    # Exercise start's backlog setup, then drive its usual tick explicitly.
    monkeypatch.setattr(scheduler, "_run_loop", lambda: None)
    scheduler.start()
    try:
        scheduler.run_due()
        assert _passes(agent) == []
        assert [seg["id"] for seg in _passes(agent, notes=True)[0].new_segments] == [
            "late", "older",
        ]
        assert repo.get_delivery_cursor(meeting_id, "notes") == high_water
    finally:
        scheduler.stop()


def test_upgrade_backfills_existing_segments_once_in_row_order(repo, db):
    from sqlalchemy import text

    meeting_id = make_meeting(repo)
    repo.add_segments([
        make_segment(meeting_id, "original", start=700),
        make_segment(meeting_id, "older", start=2),
    ])
    with db.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER meeting_segments_delivery_ai"))
        conn.execute(text("DROP TABLE meeting_segment_delivery"))
        conn.execute(text("UPDATE schema_version SET version=15"))
    db.close()

    upgraded = DatabaseManager(db_path=db.db_path)
    try:
        upgraded_repo = SqlMeetingRepository(db=upgraded)
        page = upgraded_repo.get_segments_delivery_page(meeting_id)
        assert [row["id"] for row in page] == ["original", "older"]
        seqs = [row["delivery_seq"] for row in page]
        assert seqs == sorted(seqs)
    finally:
        upgraded.close()

    reopened = DatabaseManager(db_path=db.db_path)
    try:
        page = SqlMeetingRepository(db=reopened).get_segments_delivery_page(meeting_id)
        assert [row["delivery_seq"] for row in page] == seqs
    finally:
        reopened.close()
