"""The scheduler's semantic topic-shift judge overrides Jaccard and falls back cleanly."""
import time
from unittest.mock import patch

from meeting.agent import scheduler as scheduler_mod
from meeting.agent.scheduler import CheckpointScheduler


class FakeClock:
    def __init__(self, now_s=200.0):
        self._now = now_s

    def now_s(self):
        return self._now


class FakeEngine:
    def __init__(self, segments):
        self.clock = FakeClock()
        self._segments = list(segments)
        self.store = None

    def get_transcript(self, after_start_s=-1.0, limit=None):
        return [s for s in self._segments if float(s["start_s"]) > float(after_start_s)]


FINANCE = [
    {"start_s": 85.0, "end_s": 95.0, "text": "budget forecast revenue pipeline margin earnings"},
    {"start_s": 100.0, "end_s": 115.0, "text": "quarterly finance spreadsheet ledger accounting"},
]
HIRING = [
    {"start_s": 145.0, "end_s": 155.0, "text": "hiring onboarding interview candidates staffing"},
    {"start_s": 165.0, "end_s": 185.0, "text": "recruitment culture retention engineers designers"},
]
FINANCE_AGAIN = [
    {"start_s": 145.0, "end_s": 155.0, "text": "budget forecast revenue pipeline margin earnings"},
    {"start_s": 165.0, "end_s": 185.0, "text": "quarterly finance spreadsheet ledger accounting"},
]


class RecordingJudge:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def __call__(self, older, newer):
        self.calls.append((older, newer))
        if self.exc:
            raise self.exc
        return self.result


def make(segments, judge):
    sched = CheckpointScheduler(FakeEngine(segments), agent_core=object(),
                                base_interval_s=45.0, min_interval_s=0.05, max_interval_s=60.0,
                                topic_judge=judge)
    sched._last_fire_mono = time.monotonic() - 5.0
    sched._last_shift_check_mono = 0.0
    return sched


def detect(sched):
    with patch.object(scheduler_mod, "_SHIFT_CHECK_SPACING_S", 0.0):
        return sched._detect_topic_shift()


def test_semantic_yes_overrides_similar_words():
    judge = RecordingJudge(result=0.91)
    sched = make(FINANCE + FINANCE_AGAIN, judge)  # Jaccard says "same topic"
    assert detect(sched) is True
    older, newer = judge.calls[0]
    assert "budget forecast" in older and "budget forecast" in newer


def test_semantic_no_overrides_disjoint_words():
    sched = make(FINANCE + HIRING, RecordingJudge(result=0.12))  # Jaccard says "shifted"
    assert detect(sched) is False


def test_threshold_boundary():
    assert detect(make(FINANCE + FINANCE_AGAIN, RecordingJudge(result=0.5))) is True
    assert detect(make(FINANCE + FINANCE_AGAIN, RecordingJudge(result=0.49))) is False


def test_no_answer_falls_back_to_jaccard():
    assert detect(make(FINANCE + HIRING, RecordingJudge(result=None))) is True
    assert detect(make(FINANCE + FINANCE_AGAIN, RecordingJudge(result=None))) is False


def test_judge_exception_falls_back_to_jaccard():
    assert detect(make(FINANCE + HIRING, RecordingJudge(exc=RuntimeError("boom")))) is True


def test_judge_not_asked_for_tiny_windows():
    judge = RecordingJudge(result=0.99)
    tiny = [{"start_s": 90.0, "end_s": 91.0, "text": "okay"}, {"start_s": 150.0, "end_s": 151.0, "text": "sure"}]
    assert detect(make(tiny, judge)) is False
    assert judge.calls == []


def test_without_judge_behaviour_is_unchanged():
    assert detect(make(FINANCE + HIRING, None)) is True
    assert detect(make(FINANCE + FINANCE_AGAIN, None)) is False
