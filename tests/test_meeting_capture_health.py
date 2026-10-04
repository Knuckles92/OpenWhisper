"""Signal checks distinguish live quiet buffers from absent callbacks."""

import numpy as np

from meeting.capture.health import CaptureSignalMonitor
from meeting.interfaces import CaptureBlock


def block(value: int, *, samples: int = 4000, t_mono: float = 0.0) -> CaptureBlock:
    return CaptureBlock("mic", np.full(samples, value, np.int16), 16000, t_mono)


def test_quiet_blocks_are_receiving_but_do_not_pass_guided_signal_check():
    monitor = CaptureSignalMonitor()
    monitor.reset_stream(now=0.0)
    monitor.observe(block(0), now=0.25)
    snapshot = monitor.snapshot(now=0.5)
    assert snapshot == {"receiving": True, "stalled": False,
                        "signal_windows": 0, "generation": 2}


def test_attenuated_audio_does_not_pass_but_healthy_signal_does():
    monitor = CaptureSignalMonitor()
    monitor.reset_stream(now=0.0)
    monitor.observe(block(50), now=0.25)  # 1% of a 5000-int16 voice window
    assert monitor.snapshot(now=0.3)["signal_windows"] == 0
    for now in (0.5, 0.75, 1.0):
        monitor.observe(block(5000), now=now)
    assert monitor.snapshot(now=1.1)["signal_windows"] == 1


def test_short_click_is_not_enough_and_continuous_signal_is_rate_limited():
    monitor = CaptureSignalMonitor()
    monitor.reset_stream(now=0.0)
    for now, value in ((0.25, 6000), (0.5, 0), (0.75, 6000),
                       (1.0, 6000), (1.25, 6000)):
        monitor.observe(block(value), now=now)
    assert monitor.snapshot(now=1.3)["signal_windows"] == 1
    for now in (1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25):
        monitor.observe(block(6000), now=now)
    assert monitor.snapshot(now=3.3)["signal_windows"] == 2


def test_wrong_source_at_same_level_can_pass_signal_check():
    monitor = CaptureSignalMonitor()
    for now in (1.0, 1.25, 1.5):
        monitor.observe(block(3000), now=now)
    assert monitor.snapshot(now=1.1)["signal_windows"] == 1
    # This is deliberately a limitation: amplitude alone cannot identify
    # whether the words came from the intended microphone or system output.


def test_block_stall_and_source_restart_preserve_guided_check_counter():
    monitor = CaptureSignalMonitor()
    monitor.reset_stream(now=0.0)
    assert monitor.snapshot(now=3.9)["stalled"] is False
    assert monitor.snapshot(now=4.0)["stalled"] is True
    for now in (4.1, 4.35, 4.6):
        monitor.observe(block(1000), now=now)
    assert monitor.snapshot(now=4.2)["signal_windows"] == 1
    assert monitor.snapshot(now=7.6)["stalled"] is True
    monitor.reset_stream(now=8.0)
    assert monitor.snapshot(now=8.1) == {
        "receiving": False, "stalled": False, "signal_windows": 1,
        "generation": 3,
    }


def test_paused_capture_counts_buffers_but_not_signal():
    monitor = CaptureSignalMonitor()
    monitor.reset_stream(now=0.0)
    monitor.observe(block(5000), signal_enabled=False, now=0.3)
    assert monitor.snapshot(now=0.4) == {
        "receiving": True, "stalled": False, "signal_windows": 0,
        "generation": 2,
    }
