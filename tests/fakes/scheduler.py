"""Engine, store, clock, and agent stand-ins for driving ``CheckpointScheduler``.

The scheduler reads only ``engine.clock``, ``engine.store`` and
``engine.get_transcript``; these fakes serve a fixed transcript over a meeting
clock that sits past the warm-up window.
"""
from meeting.interfaces import AgentResult, OpResult


def seeded_snapshot():
    """A dashboard with one proposed key point, a topic, and a summary."""
    return {
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


def blank_snapshot():
    """A dashboard with a topic and a summary but no cards yet."""
    return {
        "meeting_id": "m_test",
        "seq": 1,
        "cards": {},
        "topic": {"current": "seeded topic", "history": []},
        "rolling_summary": "seeded summary",
    }


def segment(seg_id, start_s, text="hello there", channel="mic"):
    """A two-second transcript row as ``engine.get_transcript`` returns it."""
    return {
        "id": seg_id,
        "start_s": start_s,
        "end_s": start_s + 2.0,
        "text": text,
        "channel": channel,
    }


class FakeClock:
    def __init__(self, now_s=200.0):
        self._now = now_s

    def now_s(self):
        return self._now


class FakeSchedulerStore:
    """Serves a fixed snapshot and records the ops applied to it."""

    def __init__(self, snapshot=None):
        self._snapshot = snapshot or seeded_snapshot()
        self.apply_calls = []

    def snapshot(self):
        return dict(self._snapshot)

    def apply(self, actor_kind, actor_id, ops):
        self.apply_calls.append((actor_kind, actor_id, ops))
        return [OpResult(ok=True, op=op) for op in ops]


class FakeSchedulerEngine:
    """A meeting engine holding a fixed transcript, 200 s in by default."""

    def __init__(self, segments=None, clock_s=200.0, snapshot=None):
        self.store = FakeSchedulerStore(snapshot)
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


class FakeNotesEngine(FakeSchedulerEngine):
    """The engine the note-taker tests drive: a meeting with no cards yet."""

    def __init__(self, segments=None):
        super().__init__(segments, snapshot=blank_snapshot())


class FakeNotesAgent:
    """Records every pass it is handed; the first ``fail_times`` fail."""

    def __init__(self, fail_times=0):
        self.calls = []
        self._fail_left = fail_times

    def checkpoint(self, payload):
        self.calls.append(payload)
        if self._fail_left > 0:
            self._fail_left -= 1
            return AgentResult(ok=False, error="forced")
        return AgentResult(
            ok=True,
            op_results=[OpResult(ok=True, op={"op": "add_item"}, seq=1)],
        )

    def is_healthy(self):
        return True
