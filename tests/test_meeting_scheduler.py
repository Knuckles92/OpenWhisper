"""
Tests for CheckpointScheduler: adaptive intervals, coalescing, Jaccard early fire.
"""
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.agent import scheduler as scheduler_mod
from meeting.agent.scheduler import CheckpointScheduler, _content_words
from meeting.interfaces import AgentResult, OpResult


class FakeStore:
    def __init__(self, snapshot=None):
        self._snapshot = snapshot or {
            "meeting_id": "m_test",
            "seq": 1,
            "cards": {
                "key_points": [{
                    "id": "it_1", "text": "seeded", "status": "proposed",
                    "evidence": ["sg_1"],
                }],
            },
            "topic": {"current": "seeded topic", "history": []},
            "rolling_summary": "seeded summary",
        }
        self.apply_calls = []

    def snapshot(self):
        return dict(self._snapshot)

    def apply(self, actor_kind, actor_id, ops):
        self.apply_calls.append((actor_kind, actor_id, ops))
        return [OpResult(ok=True, op=op) for op in ops]


class FakeClock:
    def __init__(self, now_s=200.0):
        self._now = now_s

    def now_s(self):
        return self._now


class FakeEngine:
    def __init__(self, segments=None, clock_s=200.0):
        self.store = FakeStore()
        self.clock = FakeClock(clock_s)
        self._segments = list(segments or [])

    def get_transcript(self, after_start_s=-1.0, limit=None):
        # Match repository semantics: start_s strictly greater than the cursor.
        items = [
            s for s in self._segments
            if float(s.get("start_s") or 0.0) > float(after_start_s)
        ]
        if limit is not None:
            items = items[:limit]
        return items


class FakeAgent:
    def __init__(self, block_s=0.0, fail_times=0):
        self.calls = []
        self.block_s = block_s
        self.fail_times = fail_times
        self._fail_left = fail_times
        self._entered = threading.Event()
        self._release = threading.Event()
        self._release.set()

    def checkpoint(self, payload):
        self.calls.append(payload)
        self._entered.set()
        if self.block_s > 0:
            self._release.wait(timeout=self.block_s + 2.0)
            time.sleep(0.01)
        if self._fail_left > 0:
            self._fail_left -= 1
            return AgentResult(ok=False, error="forced")
        return AgentResult(
            ok=True,
            op_results=[OpResult(ok=True, op={"op": "set_topic"}, seq=1)],
        )

    def consolidate(self, payload):
        return self.checkpoint(payload)

    def cancel(self):
        self._release.set()

    def is_healthy(self):
        return True


class TestAdaptiveInterval:
    def test_defaults_prioritize_live_dashboard_freshness(self):
        sched = CheckpointScheduler(FakeEngine(), FakeAgent())

        assert sched._initial_interval_s == 3.0
        assert sched._interval_for(2) == 20.0
        assert sched._interval_for(3) == 15.0
        assert sched._interval_for(8) == 5.0

    def test_pinned_intervals(self):
        engine = FakeEngine()
        agent = FakeAgent()
        sched = CheckpointScheduler(
            engine, agent,
            base_interval_s=45.0, min_interval_s=30.0, max_interval_s=60.0,
        )
        assert sched._interval_for(0) == 60.0   # quiet (< 3)
        assert sched._interval_for(2) == 60.0
        assert sched._interval_for(3) == 45.0   # mid
        assert sched._interval_for(7) == 45.0
        assert sched._interval_for(8) == 30.0   # pressure floor
        assert sched._interval_for(20) == 30.0


class TestCoalescing:
    def test_pending_while_in_flight_coalesce_into_next_fire(self):
        segs = [
            {"id": "sg_1", "start_s": 1.0, "end_s": 2.0, "text": "one"},
            {"id": "sg_2", "start_s": 3.0, "end_s": 4.0, "text": "two"},
        ]
        engine = FakeEngine(segs)
        agent = FakeAgent()
        agent.block_s = 5.0
        agent._release.clear()

        sched = CheckpointScheduler(
            engine, agent,
            base_interval_s=0.05, min_interval_s=0.05, max_interval_s=0.05,
        )
        sched._last_fire_mono = time.monotonic() - 1.0
        sched.start()
        try:
            sched.notify_segments(2)
            assert agent._entered.wait(2.0), "first checkpoint did not start"
            # New transcript lands while the first checkpoint is in flight;
            # the single worker coalesces it into the immediate follow-up fire.
            engine._segments.append(
                {"id": "sg_3", "start_s": 5.0, "end_s": 6.0, "text": "three"}
            )
            sched.notify_segments(1)
            agent._release.set()
            deadline = time.monotonic() + 3.0
            while len(agent.calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert len(agent.calls) >= 2
            second_ids = [s["id"] for s in agent.calls[1].new_segments]
            assert "sg_3" in second_ids
        finally:
            agent._release.set()
            sched.stop()


class TestSegmentWatermark:
    """The fetch cursor must never hide transcript from the agent.

    Mic and loopback are separate spools whose chunks reach one shared ASR
    FIFO out of order, so a segment starting well before the newest one can
    still be stored after it.
    """

    @staticmethod
    def _sched(engine, agent):
        return CheckpointScheduler(
            engine, agent,
            base_interval_s=0.05, min_interval_s=0.05, max_interval_s=0.05,
        )

    def test_out_of_order_channel_arrival_loses_nothing(self):
        # Mic chunk 0 (one long segment) is transcribed first.
        engine = FakeEngine([
            {"id": "mic_1", "start_s": 0.0, "end_s": 30.0, "text": "mic one"},
        ])
        agent = FakeAgent()
        sched = self._sched(engine, agent)

        sched._fire()
        assert [s["id"] for s in agent.calls[0].new_segments] == ["mic_1"]

        # Loopback chunk 0 covers the same wall-clock window but lands after:
        # every one of its segments starts before the mic segment's end.
        engine._segments.extend([
            {"id": "loop_1", "start_s": 0.0, "end_s": 8.0, "text": "loop one"},
            {"id": "loop_2", "start_s": 8.0, "end_s": 16.0, "text": "loop two"},
            {"id": "loop_3", "start_s": 16.0, "end_s": 24.0, "text": "loop three"},
            {"id": "loop_4", "start_s": 24.0, "end_s": 32.0, "text": "loop four"},
        ])
        sched._fire()
        card_calls = [c for c in agent.calls if not c.is_polish]
        assert len(card_calls) == 2
        assert [s["id"] for s in card_calls[1].new_segments] == [
            "loop_1", "loop_2", "loop_3", "loop_4",
        ]

    def test_sent_segments_are_never_resent(self):
        engine = FakeEngine([
            {"id": "sg_1", "start_s": 0.0, "end_s": 5.0, "text": "one"},
            {"id": "sg_2", "start_s": 5.0, "end_s": 10.0, "text": "two"},
        ])
        agent = FakeAgent()
        sched = self._sched(engine, agent)

        sched._fire()
        assert [s["id"] for s in agent.calls[0].new_segments] == ["sg_1", "sg_2"]
        # Nothing new arrived: the re-fetch window returns the same rows, all
        # of which are filtered out, so no checkpoint fires at all.
        sched._fire()
        assert len(agent.calls) == 1

    def test_failed_checkpoint_retries_the_same_segments(self):
        engine = FakeEngine([
            {"id": "sg_1", "start_s": 0.0, "end_s": 5.0, "text": "one"},
        ])
        agent = FakeAgent(fail_times=1)
        sched = self._sched(engine, agent)

        sched._fire()
        sched._fire()
        assert len(agent.calls) == 2
        assert [s["id"] for s in agent.calls[1].new_segments] == ["sg_1"]

    def test_offline_scheduler_recovers_after_a_successful_retry(self):
        engine = FakeEngine([
            {"id": "sg_1", "start_s": 0.0, "end_s": 5.0, "text": "one"},
        ])
        agent = FakeAgent(fail_times=3)
        health = []
        sched = CheckpointScheduler(engine, agent, on_health=health.append)

        for _ in range(3):
            sched._pending_segments = 1
            sched._fire()
        assert health == [False]
        assert sched._online is False

        sched._pending_segments = 1
        sched._fire()
        assert health == [False, True]
        assert sched._online is True
        assert sched._consecutive_failures == 0

    def test_empty_checkpoint_backfills_blank_dashboard(self):
        empty_store = FakeStore({
            "meeting_id": "m_test",
            "seq": 1,
            "cards": {"key_points": []},
            "topic": {"current": "", "history": []},
            "rolling_summary": "",
        })
        engine = FakeEngine([
            {
                "id": "sg_1", "start_s": 0.0, "end_s": 5.0,
                "text": "We should pack up and try the griddle.",
            },
        ])
        engine.store = empty_store

        class QuietAgent(FakeAgent):
            def checkpoint(self, payload):
                self.calls.append(payload)
                return AgentResult(ok=True, op_results=[])

        agent = QuietAgent()
        sched = CheckpointScheduler(engine, agent)
        sched._pending_segments = 1

        with patch(
            "meeting.state.repair.repair_meeting_state", return_value=2,
        ) as repair:
            sched._fire()
            repair.assert_called_once()
            assert repair.call_args.args[0] is empty_store
            assert repair.call_args.args[1][0]["id"] == "sg_1"

    def test_prepare_for_end_never_blocks_on_agent_cancel(self):
        agent = FakeAgent()
        canceled = []
        agent.cancel = lambda: canceled.append(True)
        sched = CheckpointScheduler(FakeEngine(), agent)
        sched.start()
        try:
            sched.prepare_for_end()
            assert sched._consolidating is True
            assert sched._stop_event.is_set()
            assert canceled == []
        finally:
            sched.stop()

    def test_transcript_fetch_failure_restores_claimed_work(self):
        class FailingEngine(FakeEngine):
            def get_transcript(self, after_start_s=-1.0, limit=None):
                raise RuntimeError("database busy")

        sched = self._sched(FailingEngine(), FakeAgent())
        sched._pending_segments = 4
        sched._fire()
        assert sched._pending_segments == 4
        assert sched._consecutive_failures == 1

    def test_sent_id_set_is_pruned_outside_the_refetch_window(self):
        engine = FakeEngine()
        agent = FakeAgent()
        sched = self._sched(engine, agent)

        with patch.object(scheduler_mod, "_REFETCH_WINDOW_S", 20.0):
            for i in range(12):
                start_s = float(i * 10)
                engine._segments.append({
                    "id": f"sg_{i}", "start_s": start_s,
                    "end_s": start_s + 10.0, "text": f"seg {i}",
                })
                sched._fire()
            # Ten-second segments with a 20s window: only the newest handful
            # can ever be re-fetched, so the set stays small forever.
            assert len(sched._sent_starts) <= 3
            assert "sg_0" not in sched._sent_starts
            assert "sg_11" in sched._sent_starts
        card_calls = [c for c in agent.calls if not c.is_polish]
        assert len(card_calls) == 12
        assert any(c.is_polish for c in agent.calls)


class TestConsolidationRace:
    def test_consolidation_skipped_when_worker_will_not_stop(self):
        engine = FakeEngine([
            {"id": "sg_1", "start_s": 0.0, "end_s": 5.0, "text": "one"},
        ])
        agent = FakeAgent()
        sched = CheckpointScheduler(engine, agent)

        # A worker thread that ignores stop() and cancel() entirely.
        wedged = threading.Event()
        thread = threading.Thread(target=lambda: wedged.wait(30.0), daemon=True)
        thread.start()
        sched._thread = thread
        try:
            with patch.object(scheduler_mod, "_CONSOLIDATION_JOIN_S", 0.05):
                sched.run_consolidation(timeout_s=1.0)
            # Two agent runs sharing one core is worse than a missing pass.
            assert agent.calls == []
        finally:
            wedged.set()
            thread.join(timeout=5.0)

    def test_consolidation_runs_once_the_worker_is_stopped(self):
        engine = FakeEngine([
            {"id": "sg_1", "start_s": 0.0, "end_s": 5.0, "text": "one"},
        ])
        agent = FakeAgent()
        sched = CheckpointScheduler(engine, agent)
        with patch.object(scheduler_mod, "_CONSOLIDATION_JOIN_S", 0.05):
            sched.run_consolidation(timeout_s=5.0)
        assert len(agent.calls) == 1
        assert agent.calls[0].is_consolidation is True


class TestTopicShift:
    def test_content_words_filters_stopwords(self):
        words = _content_words(
            "Yeah okay we should discuss the quarterly budget review process"
        )
        assert "yeah" not in words
        assert "okay" not in words
        assert "budget" in words
        assert "quarterly" in words

    def test_jaccard_topic_shift_early_fire(self):
        # Two 60s windows with disjoint content words (jaccard ~ 0).
        # Cursor is now_s-120; repository uses start_s > cursor, so keep
        # older starts strictly above 80 when now_s=200.
        older = [
            {"start_s": 85.0, "end_s": 95.0,
             "text": "budget forecast revenue pipeline margin earnings"},
            {"start_s": 100.0, "end_s": 115.0,
             "text": "quarterly finance spreadsheet ledger accounting"},
        ]
        newer = [
            {"start_s": 145.0, "end_s": 155.0,
             "text": "hiring onboarding interview candidates staffing"},
            {"start_s": 165.0, "end_s": 185.0,
             "text": "recruitment culture retention engineers designers"},
        ]
        engine = FakeEngine(older + newer, clock_s=200.0)
        agent = FakeAgent()
        sched = CheckpointScheduler(
            engine, agent,
            base_interval_s=45.0, min_interval_s=0.05, max_interval_s=60.0,
        )
        # Elapsed past min but not past base (45s) — only topic shift can fire
        sched._last_fire_mono = time.monotonic() - 5.0
        sched._last_shift_check_mono = 0.0
        sched._pending_segments = 4

        with patch.object(scheduler_mod, "_SHIFT_CHECK_SPACING_S", 0.0):
            assert sched._detect_topic_shift() is True

        # Similar windows: no shift
        similar = older + [
            {"start_s": 145.0, "end_s": 155.0,
             "text": "budget forecast revenue pipeline margin earnings"},
            {"start_s": 165.0, "end_s": 185.0,
             "text": "quarterly finance spreadsheet ledger accounting"},
        ]
        engine._segments = similar
        sched._last_shift_check_mono = 0.0
        with patch.object(scheduler_mod, "_SHIFT_CHECK_SPACING_S", 0.0):
            assert sched._detect_topic_shift() is False

    def test_topic_shift_triggers_fire_when_interval_not_due(self):
        older = [
            {"start_s": 85.0, "end_s": 95.0,
             "text": "budget forecast revenue pipeline margin earnings"},
            {"start_s": 100.0, "end_s": 115.0,
             "text": "quarterly finance spreadsheet ledger accounting"},
        ]
        newer = [
            {"start_s": 145.0, "end_s": 155.0,
             "text": "hiring onboarding interview candidates staffing"},
            {"start_s": 165.0, "end_s": 185.0,
             "text": "recruitment culture retention engineers designers"},
        ]
        engine = FakeEngine(older + newer, clock_s=200.0)
        agent = FakeAgent()
        sched = CheckpointScheduler(
            engine, agent,
            base_interval_s=45.0, min_interval_s=0.05, max_interval_s=60.0,
        )
        sched._last_fire_mono = time.monotonic() - 5.0  # < base 45s
        sched._last_shift_check_mono = 0.0
        sched.start()
        try:
            with patch.object(scheduler_mod, "_SHIFT_CHECK_SPACING_S", 0.0):
                sched.notify_segments(4)
                deadline = time.monotonic() + 3.0
                while not agent.calls and time.monotonic() < deadline:
                    time.sleep(0.05)
            assert agent.calls, "topic shift should early-fire a checkpoint"
        finally:
            sched.stop()
