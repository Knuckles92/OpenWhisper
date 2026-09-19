"""Pulse scheduling and final catch-up, with deterministic local judgments."""
from types import SimpleNamespace

import pytest

from meeting.engine import MeetingEngine, MeetingEngineOptions
from meeting.live_signals import LiveSignals
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore


class Queue:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)

    def shutdown(self, **kwargs):
        self.jobs.clear()


def row(sid, start):
    return dict(id=sid, start_s=start, end_s=start + 3, text="The budget is 1000 dollars.")


class Repo:
    def __init__(self, rows):
        self.rows = rows

    def get_segments(self, mid, after_start_s=-1, limit=None):
        return [dict(r) for r in self.rows if r["start_s"] > after_start_s][:limit]

    def get_segment(self, mid, sid):
        return next((dict(r) for r in self.rows if r["id"] == sid), None)


@pytest.fixture
def signals(monkeypatch):
    monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled", lambda _: True)
    target = MeetingStateStore(MeetingState("m_test", cloud_enabled=True))
    repository, queue, calls = Repo([row("first", 8)]), Queue(), []

    def ask(state, questions, **kwargs):
        calls.append(state)
        sid = next(iter(state["passages"]))
        return {"number": {"noul": .95}, "number_anchor": {"choice": sid}}

    worker = LiveSignals(target, repository, SimpleNamespace(ask=ask), lambda: True, executor=queue)
    return worker, repository, queue, calls


def test_busy_worker_retains_all_completed_minutes(signals):
    worker, repo, queue, calls = signals
    repo.rows += [row("second", 75), row("third", 125)]
    worker.observe([], frontier=60)
    worker.observe([], frontier=180)
    assert len(queue.jobs) == 1 and not calls
    queue.run()
    assert [next(iter(call["passages"])) for call in calls] == ["first", "second", "third"]
    assert len(worker.store.snapshot()["live_highlights"]) == 3


def test_silence_advances_check_and_duplicate_does_not_call_again(signals):
    worker, repo, queue, calls = signals
    worker.observe(repo.rows)
    assert not queue.jobs
    worker.observe([], frontier=61)
    queue.run()
    assert len(calls) == 1
    worker.observe(repo.rows)
    queue.run()
    assert len(calls) == 1


def test_late_channel_rechecks_the_earlier_minute(signals):
    worker, repo, queue, calls = signals
    worker.observe([], frontier=120)
    queue.run()
    repo.rows.append(row("late", 30))
    worker.observe([repo.rows[-1]])
    queue.run()
    assert len(calls) == 2 and "late" in calls[-1]["passages"]
    assert len(worker.store.snapshot()["live_highlights"]) == 1


def test_revised_evidence_is_checked_again_and_old_pulse_removed(signals):
    worker, repo, queue, calls = signals
    worker.observe([], frontier=60)
    queue.run()
    repo.rows[0]["text"] = "No budget was mentioned."
    worker.judge.ask = lambda *a, **k: {"number": {"noul": .1}}
    worker.observe(repo.rows)
    queue.run()
    assert worker.store.snapshot()["live_highlights"] == []


def test_failed_live_request_can_be_retried_after_a_late_arrival(signals):
    worker, repo, queue, calls = signals
    ask = worker.judge.ask
    worker.judge.ask = lambda *a, **k: None
    worker.observe([], frontier=60)
    queue.run()
    worker.judge.ask = ask
    worker.observe(repo.rows)
    queue.run()
    assert len(worker.store.snapshot()["live_highlights"]) == 1


def test_final_check_includes_short_meeting_and_partial_last_minute(signals):
    worker, repo, queue, calls = signals
    repo.rows += [row("tail", 139)]
    worker.store.update_runtime_fields(status="ended")
    worker.finalize()
    assert [p["start_s"] for p in worker.store.snapshot()["live_highlights"]] == [8, 139]
    assert all(not call["known_questions"] for call in calls)
    assert not worker.store.snapshot()["questions"]
    worker.finalize()
    assert len(calls) == 2


def test_final_checks_obey_total_request_budget(signals, monkeypatch):
    worker, repo, queue, calls = signals
    repo.rows += [row("second", 75)]
    worker.store.update_runtime_fields(status="ended")
    clock = [0.0]
    monkeypatch.setattr("meeting.live_signals.time.monotonic", lambda: clock[0])
    ask = worker.judge.ask

    def slow(state, questions, timeout_s):
        assert timeout_s == 1.5
        clock[0] += 2
        return ask(state, questions)

    worker.judge.ask = slow
    worker.finalize(timeout_s=1.5)
    assert len(calls) == 1


@pytest.mark.parametrize("switch", ["cloud", "feature"])
def test_final_results_are_discarded_if_consent_is_revoked(signals, monkeypatch, switch):
    worker, repo, queue, calls = signals
    worker.store.update_runtime_fields(status="ended")
    allowed = [True]
    worker.allowed = lambda: allowed[0]
    ask = worker.judge.ask

    def revoke(state, questions, **kwargs):
        if switch == "cloud":
            allowed[0] = False
        else:
            monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled", lambda _: False)
        return ask(state, questions)

    worker.judge.ask = revoke
    worker.finalize()
    assert not worker.store.snapshot()["live_highlights"]


def test_only_final_system_pulses_can_write_after_end(signals):
    worker, *_ = signals
    worker.store.update_runtime_fields(status="ended")
    op = {"op": "publish_highlights", "pulses": [], "minute": 0}
    assert not worker.store.apply("system", "live-signals", [op])[0].ok
    op["final"] = True
    assert not worker.store.apply("host", "me", [op])[0].ok
    assert worker.store.apply("system", "live-signals", [op])[0].ok
    worker.store.update_runtime_fields(cloud_enabled=False)
    assert not worker.store.apply("system", "live-signals", [op])[0].ok


def test_engine_final_pulses_do_not_require_the_notes_agent(signals, monkeypatch):
    worker, repo, queue, calls = signals
    worker.store.update_runtime_fields(status="ended")
    monkeypatch.setattr("services.settings.resolve_typesafe_feature_enabled", lambda _: True)
    engine = MeetingEngine(MeetingEngineOptions(), repository=repo)
    engine.store = worker.store
    engine._fast_features_allowed = lambda: True
    engine._typesafe_judge = lambda: worker.judge
    assert engine._scheduler is None
    engine._finalize_highlights()
    assert len(worker.store.snapshot()["live_highlights"]) == 1


def test_final_pulses_persist_and_broadcast(repo, monkeypatch):
    monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled", lambda _: True)
    from meeting.interfaces import TranscriptSegment
    repo.create_meeting(id="m_final", title="Test", status="ended", started_at="2026-09-19T20:00:00Z",
                        host_token="host", guest_token="guest", cloud_enabled=True, spool_dir="unused")
    repo.add_segments([TranscriptSegment("sg_final", "m_final", None, "mic", 8, 12, "The budget is 1000 dollars.")])
    target = MeetingStateStore(MeetingState("m_final", status="ended", cloud_enabled=True), repository=repo,
                               segment_exists=lambda sid: repo.get_segment("m_final", sid) is not None)
    events = []
    target.subscribe(lambda seq, results: events.extend(results))
    judge = SimpleNamespace(ask=lambda *a, **k: {"number": {"noul": .95}, "number_anchor": {"choice": "sg_final"}})
    worker = LiveSignals(target, repo, judge, lambda: True, executor=Queue())
    worker.finalize()
    import json
    saved = json.loads(repo.get_meeting("m_final")["state_json"])
    assert saved["live_highlights"][0]["segment_id"] == "sg_final"
    assert events[0].effect["entity"] == "live_highlights"


@pytest.mark.parametrize("status", ["active", "canceled", "needs_recovery"])
def test_final_check_does_not_send_in_ineligible_state(signals, status):
    worker, repo, queue, calls = signals
    worker.store.update_runtime_fields(status=status)
    worker.finalize()
    assert calls == []


def test_shutdown_during_request_discards_result_and_pending_windows(signals):
    worker, repo, queue, calls = signals
    repo.rows += [row("second", 75)]
    ask = worker.judge.ask

    def stop(state, questions, **kwargs):
        worker.shutdown()
        return ask(state, questions)

    worker.judge.ask = stop
    worker.observe([], frontier=120)
    queue.run()
    assert len(calls) == 1
    assert not worker.store.snapshot()["live_highlights"]
    assert not worker.pending and not worker.busy


def test_empty_committed_audio_advances_pulse_frontier(signals, monkeypatch):
    from meeting.interfaces import SpooledChunk
    from unittest.mock import Mock
    worker, repo, *_ = signals
    engine = MeetingEngine(MeetingEngineOptions(), repository=repo)
    repo.commit_chunk_transcription = lambda *a: ([], True)
    engine._live_signals = Mock()
    engine._start_fast_features = lambda: None
    engine._maybe_revise_transcript = lambda chunk: None
    chunk = SpooledChunk(1, "m_test", "mic", 1, "unused", 55, 6, 16000)
    engine._on_chunk_result(chunk, [])
    engine._live_signals.observe.assert_called_once_with([], frontier=61)


def test_rolling_revision_reaches_pulse_worker(signals):
    from unittest.mock import Mock
    worker, repo, *_ = signals
    engine = MeetingEngine(MeetingEngineOptions(), repository=repo)
    engine._live_signals = Mock()
    revised = dict(repo.rows[0], speaker_participant_id="me")
    engine._publish_revise_result({"items": [revised], "removed_ids": []})
    engine._live_signals.observe.assert_called_once_with([revised])


@pytest.mark.parametrize("probability,anchor,expected", [
    (.67, "first", 0), (.79, "first", 0), (.80, "first", 1),
    (.94, "first", 1), (.99, "missing", 0),
])
def test_clearer_definitions_preserve_threshold_and_real_evidence(probability, anchor, expected):
    from meeting.live_signals import window_request, window_ops
    state, _ = window_request([row("first", 8)], {}, radar=False)
    answers = {"number": {"noul": probability}, "number_anchor": {"choice": anchor, "confidence": 1.0}}
    pulses = [p for op in window_ops(0, state, answers, {}) for p in op.get("pulses", [])]
    assert len(pulses) == expected
