"""
Tests for MeetingClock: pause credit, timestamp conversion, recovery re-anchor.
"""
from types import SimpleNamespace

import pytest

import meeting.clock as clock_module
from meeting.clock import MeetingClock


@pytest.fixture
def monotonic(monkeypatch):
    ticks = SimpleNamespace(value=10_000.0)

    def advance(seconds):
        ticks.value += seconds

    ticks.advance = advance
    # Replace this module's clock source, leaving pytest and real worker
    # deadlines on their own monotonic clocks.
    monkeypatch.setattr(clock_module, "time", SimpleNamespace(monotonic=lambda: ticks.value))
    return ticks


class TestMeetingClock:
    def test_zero_before_start(self, monotonic):
        clock = MeetingClock()
        assert clock.now_s() == 0.0
        assert not clock.is_running
        assert clock.meeting_time(monotonic.value) == 0.0

    def test_advances_after_start(self, monotonic):
        clock = MeetingClock()
        clock.start()
        monotonic.advance(0.05)
        assert clock.now_s() == pytest.approx(0.05)
        assert clock.is_running
        assert clock.started_at_iso

    def test_pause_freezes_meeting_time(self, monotonic):
        clock = MeetingClock()
        clock.start()
        monotonic.advance(0.05)
        clock.pause()
        frozen = clock.now_s()
        monotonic.advance(0.08)
        assert clock.now_s() == frozen
        # Timestamps taken during the pause resolve to the pause instant
        assert clock.meeting_time(monotonic.value) == frozen

    def test_resume_credits_paused_span(self, monotonic):
        clock = MeetingClock()
        clock.start()
        monotonic.advance(0.05)
        before_pause = clock.now_s()
        clock.pause()
        monotonic.advance(0.1)
        clock.resume()
        after = clock.now_s()
        # Scheduler delays before pause belong to meeting time; only the
        # paused span should disappear.
        assert after == pytest.approx(before_pause)
        assert clock.paused_total_s() == pytest.approx(0.1)

    def test_pause_resume_idempotent(self, monotonic):
        clock = MeetingClock()
        clock.start()
        clock.resume()  # not paused: no-op
        clock.pause()
        clock.pause()   # double pause: no-op
        clock.resume()
        clock.resume()  # double resume: no-op
        assert clock.is_running
        assert not clock.is_paused

    def test_recovery_reanchor_continues_timeline(self, monotonic):
        clock = MeetingClock()
        clock.resume_from_recovery(120.0)
        now = clock.now_s()
        assert now == pytest.approx(120.0)
        monotonic.advance(0.05)
        assert clock.now_s() == pytest.approx(now + 0.05)
