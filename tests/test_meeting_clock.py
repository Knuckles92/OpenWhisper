"""
Tests for MeetingClock: pause credit, timestamp conversion, recovery re-anchor.
"""
import time

from meeting.clock import MeetingClock

class TestMeetingClock:
    def test_zero_before_start(self):
        clock = MeetingClock()
        assert clock.now_s() == 0.0
        assert not clock.is_running
        assert clock.meeting_time(time.monotonic()) == 0.0

    def test_advances_after_start(self):
        clock = MeetingClock()
        clock.start()
        time.sleep(0.05)
        assert 0.03 < clock.now_s() < 1.0
        assert clock.is_running
        assert clock.started_at_iso

    def test_pause_freezes_meeting_time(self):
        clock = MeetingClock()
        clock.start()
        time.sleep(0.05)
        clock.pause()
        frozen = clock.now_s()
        time.sleep(0.08)
        assert abs(clock.now_s() - frozen) < 0.005
        # Timestamps taken during the pause resolve to the pause instant
        assert abs(clock.meeting_time(time.monotonic()) - frozen) < 0.005

    def test_resume_credits_paused_span(self):
        clock = MeetingClock()
        clock.start()
        time.sleep(0.05)
        before_pause = clock.now_s()
        clock.pause()
        time.sleep(0.1)
        clock.resume()
        after = clock.now_s()
        # Scheduler delays before pause belong to meeting time; only the
        # paused span should disappear.
        assert abs(after - before_pause) < 0.05
        assert clock.paused_total_s() >= 0.09

    def test_pause_resume_idempotent(self):
        clock = MeetingClock()
        clock.start()
        clock.resume()  # not paused: no-op
        clock.pause()
        clock.pause()   # double pause: no-op
        clock.resume()
        clock.resume()  # double resume: no-op
        assert clock.is_running
        assert not clock.is_paused

    def test_recovery_reanchor_continues_timeline(self):
        clock = MeetingClock()
        clock.resume_from_recovery(120.0)
        now = clock.now_s()
        assert 119.9 < now < 121.0
        time.sleep(0.05)
        assert clock.now_s() > now
